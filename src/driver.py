#!/usr/bin/env python3
"""
Treiberkern für den Plustek ePhoto Z300.

Fährt die Scansequenz aus `sequence.py` und versorgt sie mit den Tabellen aus
`tables.py`. Die Slope- und Gammatabellen werden berechnet, die Shading-Daten
aus dem SPI-Flash des Geräts gelesen — es wird kein Herstellermaterial benötigt.

Schichten:
    ephoto.py     Registerzugriff (Control-Transfers)
    bulk.py       Bulk-Transport, SPI-Flash
    tables.py     Slope, Gamma, Shading
    sequence.py   Ablauf als Daten
    driver.py     dieses Modul — führt den Ablauf aus
    postprocess.py Bildaufbereitung
"""
import struct
import sys
import time
from pathlib import Path

import usb.core

sys.path.insert(0, str(Path(__file__).parent))
from ephoto import EPhoto  # noqa: E402
from sequence import SEQUENCES, GEOMETRY  # noqa: E402
from tables import (gamma_table, slope_for, slope_table_600,  # noqa: E402
                    shading_from_flash)

REQ_OUT = 0x40
BREQUEST = 0x04
VALUE_WREG, VALUE_BUFFER = 0x83, 0x82
EP_IN, EP_OUT = 0x81, 0x02
REG_STATUS, DOCSENSOR, MOTORENB = 0x101, 0x08, 0x01
REG_MOTOR = 0x02

def geometry(dpi=300):
    return GEOMETRY[dpi]


BYTES_PER_LINE = 7650          # 300 dpi; für 600 dpi geometry() nutzen

CAPTURED = Path(__file__).resolve().parent.parent / "vendor/tables"


class ScanError(RuntimeError):
    pass


def _wreg(dev, hexpairs):
    data = bytes.fromhex(hexpairs)
    dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_WREG, 0, data, 1000)


def _cmd(dev, value, index, hexdata):
    dev.dev.ctrl_transfer(REQ_OUT, BREQUEST if value != 0x8C else 0x0C,
                          value, index, bytes.fromhex(hexdata), 1000)


def _bufset(dev, addr, length, index):
    dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_BUFFER, index,
                          struct.pack("<II", addr, length), 1000)


def build_tables(dev, shading="flash", dpi=300):
    """Die sieben Downloadblöcke bereitstellen."""
    slope, gam = slope_for(dpi), gamma_table()
    n = GEOMETRY[dpi]["shading"]
    t = {"slope0": slope, "slope1": slope, "slope2": slope,
         "gamma_r": gam, "gamma_g": gam, "gamma_b": gam}
    if shading == "flash":
        t["shading"] = shading_from_flash(dev, dpi, n)
    elif shading == "captured" and dpi == 300:
        t["shading"] = (CAPTURED / "dl00_10014000.bin").read_bytes()
    elif shading == "none":
        t["shading"] = b"\x00" * n
    elif shading == "flat":
        # neutrale Kennlinie: Dunkelwert 0, Einheitsverstärkung.
        # Die Flash-Werte liegen zwischen 0x4000 und 0x9c00 — 0x4000 ist
        # demnach die Eins.  Dient der Fehlersuche: bleiben die Farbbänder
        # damit weg, liegt es an den Kalibrierdaten bzw. ihrer Ausrichtung.
        t["shading"] = struct.pack("<%dH" % (n // 2),
                                   *([0, 0x4000] * (n // 4)))
    else:
        raise ValueError(f"Shading-Quelle {shading!r} nicht für {dpi} dpi")
    return t


def paper_present(dev):
    return not (dev.read_reg(REG_STATUS) & DOCSENSOR)


def wait_for_paper(dev, seconds, on_wait=None):
    if paper_present(dev):
        return True
    if on_wait:
        on_wait()
    t0 = time.time()
    while time.time() - t0 < seconds:
        if paper_present(dev):
            return True
        time.sleep(0.05)
    return False


def settle(dev, max_seconds=12.0):
    """Nach dem Scan den Auswurf abwarten und den Motor sicher abschalten.

    Der Vendor-Ablauf bestromt am Ende den Motor, um das Blatt auszuwerfen
    (INI: EjectPaper = 6500 Schritte).  Bei 600 dpi läuft der Motor mit
    11000 µs je Schritt statt 1500 — derselbe Auswurf dauert damit rund
    70 statt 10 Sekunden und klingt entsprechend gequält.

    Deshalb: warten bis das Blatt draussen ist und der Motor steht, sonst nach
    `max_seconds` die Endstufe abschalten.  Das Blatt hat den Scanbereich zu
    diesem Zeitpunkt ohnehin schon verlassen.
    """
    t0 = time.time()
    while time.time() - t0 < max_seconds:
        st = dev.read_reg(REG_STATUS)
        if not (st & MOTORENB):
            return time.time() - t0, False
        time.sleep(0.05)
    dev.write_reg(REG_MOTOR, 0x00)          # Endstufe aus
    dev.write_reg(REG_MOTOR, 0x78)          # Ruhezustand wie im Init-Block
    return time.time() - t0, True


# Abstand Papiersensor -> Scanzeile.  INI: SheetfedLoadPaperStep = 930 Schritte
# bei MOTORDPI = 300, also rund 79 mm.  Zeilen entsprechen 1:1 der Auflösung,
# der Wert skaliert daher mit dpi.
SENSOR_TO_SCANLINE_300 = 930


def scan(dev, tables, progress=None, timeout_ms=6000, dpi=300, auto_stop=True):
    """Die Sequenz fahren und den rohen Bilddatenstrom liefern.

    Mit `auto_stop` endet der Scan, sobald das Blatt den Papiersensor verlassen
    hat und die Strecke bis zur Scanzeile nachgeführt wurde — statt stur die
    volle A4-Länge aus der mitgeschnittenen Sequenz durchzufahren.  Ein kleines
    Foto braucht so nur einen Bruchteil der Zeit und liefert kein halbleeres
    Bild.

    Der Ausstieg erfolgt zwischen zwei Blöcken; jeder Block hat sein eigenes
    BUFSET, es bleibt also keine Transaktion offen.
    """
    SEQ = SEQUENCES[dpi]
    stride = GEOMETRY[dpi]["stride"]

    # Die mitgeschnittene Sequenz beschreibt nur die Scan-Slots 0-2.  Die
    # Schnellfahr-Slots 3 und 4 nutzt der Auswurf am Ende — stehen dort
    # uninitialisierte SRAM-Werte, fährt der Motor mit zufälligen
    # Schrittintervallen und mahlt hörbar.  Der Vendor-Treiber schreibt sie
    # beim Öffnen des Geräts, also ausserhalb des mitgeschnittenen Fensters.
    fast = slope_table_600()          # Anlauf 38461, dann konstant 11000
    for addr in (0x1000C000, 0x10010000):
        _bufset(dev, addr, len(fast), 1)
        dev.dev.write(EP_OUT, fast, 2000)
    trail_lines = SENSOR_TO_SCANLINE_300 * dpi // 300
    paper_gone_at = None
    reads = [i for i, s in enumerate(SEQ) if s[0] == "R"]
    tail = reads[-1] + 1
    data = bytearray()
    stop_now = False
    t0 = time.time()
    try:
        for step in SEQ:
            kind = step[0]
            if kind == "W":
                _wreg(dev, step[1])
            elif kind == "C":
                _cmd(dev, step[1], step[2], step[3])
            elif kind == "D":
                blob = tables[step[1]]
                _bufset(dev, step[2], len(blob), 1)
                dev.dev.write(EP_OUT, blob, 2000)
            elif kind == "R":
                if stop_now:
                    continue
                _, addr, want, index = step
                _bufset(dev, addr, want, index)
                # auf Paketgrösse aufrunden, sonst liefert libusb ERRNO 75
                room = (want + 511) // 512 * 512
                data += bytes(dev.dev.read(EP_IN, room, timeout_ms))[:want]
                if progress:
                    progress(len(data), time.time() - t0)
                if auto_stop:
                    lines = len(data) // stride
                    if paper_gone_at is None:
                        if dev.read_reg(REG_STATUS) & DOCSENSOR:
                            paper_gone_at = lines
                    elif lines - paper_gone_at >= trail_lines:
                        stop_now = True
    finally:
        # Abschluss immer fahren: eine offene Lesetransaktion legt das Gerät lahm
        for step in SEQ[tail:]:
            try:
                if step[0] == "W":
                    _wreg(dev, step[1])
                elif step[0] == "C":
                    _cmd(dev, step[1], step[2], step[3])
            except usb.core.USBError:
                pass
        try:
            el, cut = settle(dev)
            if progress:
                print(f'  Auswurf: {el:.1f} s' + (' (abgeschaltet)' if cut else ' (von selbst gestoppt)'))
        except usb.core.USBError:
            pass
    return bytes(data)


def open_device():
    return EPhoto(allow_motion=True)
