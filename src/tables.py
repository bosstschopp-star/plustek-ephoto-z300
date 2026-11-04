#!/usr/bin/env python3
"""
Die Tabellen erzeugen, die der Scanner vor jedem Scan ins SRAM braucht.

Bisher wurden sie als Binärblobs aus einem USB-Mitschnitt mitgeliefert. Hier
werden sie stattdessen berechnet beziehungsweise aus dem Gerät selbst gelesen —
das macht den Treiber unabhängig von fremdem Material.

    Gamma    berechnet, trivial
    Slope    berechnet aus den Kurvenparametern
    Shading  aus dem SPI-Flash des Geräts gelesen (gerätespezifische
             Kalibrierung, keine Herstellersoftware)
"""
import pathlib
import struct

# --- Gamma -----------------------------------------------------------------
# Der Vendor lädt eine lineare Kennlinie: Eintrag[i] = (i << 8) | 0xFF.
GAMMA_ADDR = (0x01000000, 0x01000200, 0x01000400)


def gamma_table(n=256):
    """Lineare Kennlinie.

    Der Vendor nutzt (i << 8) | 0xFF, ab i >= 128 jedoch einen Zähler mehr,
    und deckelt beim Maximum. Der Unterschied ist ein Bit von 65535 und
    bildlich bedeutungslos — hier trotzdem exakt nachgebildet.
    """
    vals = [min(0xFFFF, ((i << 8) | 0xFF) + (1 if i >= n // 2 else 0))
            for i in range(n)]
    return struct.pack(f"<{n}H", *vals)


# --- Slope (Motorrampe) ----------------------------------------------------
# Aus der Vendor-Tabelle abgeleitet:  T[i] = C * i^(-e)  für i >= 1,
# gedeckelt beim Plateau.  Die Parameter stehen in der Gerätekonfiguration
# unter [Motor]:
#
#     CURVE = 20,3000,250
#             │  │    └── e = 100/250 = 0,4
#             │  └─────── Plateau = 3000/2 = 1500 µs
#             └────────── Rampenlänge (~21 Stufen bis zum Plateau)
#
# Der erste Eintrag folgt dem Gesetz nicht (i=0 wäre unendlich) und ist der
# Anlaufwert des Motors: 25641 µs = 39 Schritte/s.
#
# Nachgerechnet gegen die mitgeschnittene Tabelle: max. 0,06 % Abweichung.
SLOPE_ADDR = (0x10000000, 0x10004000, 0x10008000)
SLOPE_START_US = 25641
SLOPE_C = 5104.0

# 600 dpi fährt das Speed2-Profil: Anlauf 38461 µs, danach durchgehend 11000 µs
# (kein Rampenverlauf).  Aus dem Bulk-OUT des Vendor-Laufs.
SLOPE_600 = (38461, 11000)


def slope_table(plateau_us=1500, exponent=0.4, n=256,
                start_us=SLOPE_START_US, c=SLOPE_C):
    vals = [start_us]
    for i in range(1, n):
        t = round(c * i ** -exponent)
        vals.append(max(plateau_us, min(0xFFFF, t)))
    return struct.pack(f"<{n}H", *vals)


def slope_table_600(n=256):
    """Speed2-Rampe: ein Anlaufwert, danach konstante Periode."""
    start, plateau = SLOPE_600
    return struct.pack(f"<{n}H", start, *([plateau] * (n - 1)))


def slope_for(dpi, n=256):
    return slope_table_600(n) if dpi == 600 else slope_table(n=n)


def slope_from_curve(curve, n=256, start_us=SLOPE_START_US):
    """Rampe aus einem INI-Tripel wie "20,3000,250" erzeugen."""
    steps, period, shape = (int(x) for x in str(curve).split(","))
    plateau = period // 2
    exponent = 100.0 / shape
    c = plateau * (steps + 1.4) ** exponent      # Rampenlänge empirisch
    return slope_table(plateau, exponent, n, start_us, c)


# --- Shading ---------------------------------------------------------------
# Der Kalibrierblock liegt im SPI-Flash des Geräts, je Auflösung eine Bank
# (siehe docs/PROTOCOL.md, 4a):  300 dpi -> Bank 2, 600 dpi -> Bank 3.
SHADING_ADDR = 0x10014000
SHADING_FLASH = {300: (0x20000, 0x8000), 600: (0x30000, 0x10000)}
SHADING_LEN = {300: 31080, 600: 62168}
# Das Shading-SRAM ist blockweise organisiert: je 42 Pixel x 12 Byte = 504 Byte
# Nutzdaten + 8 Byte Füller = 512 Byte, der letzte Block verkürzt.  Der Vendor
# lädt die Flash-Daten unverändert in diesem Format (BEFUND_farbbaender.md):
#   300 dpi: 60 x 512 + 30 x 12 = 31080,  600 dpi: 121 x 512 + 18 x 12 = 62168
SHADING_BLOCK_PX = 42
SHADING_BLOCK = 512


def shading_from_flash(dev, dpi=300, length=None):
    """Kalibrierdaten aus dem Flash lesen und im SRAM-Blockformat ablegen.

    Der Flash speichert je Bildpixel 12 Byte (3 Kanäle x (Dunkel, Gain) u16).
    Das Gerät erwartet sie in Blöcken zu 42 Pixeln (504 Byte) mit je 8 Byte
    Füller.  Lückenlos geschrieben, rotiert die Kanalzuordnung alle 42 Pixel
    um 2 Kanäle — das waren die senkrechten Farbbänder (PROTOCOL §10).

    Gegen den mitgeschnittenen Vendor-Download geprüft
    (vendor/tables/shading_0x10014000.bin): 30600/30600 Nutzbyte identisch.
    Die frühere Abbildung `Bildspalte = Eintrag * 2550/2590` war die lineare
    Näherung dieses Schemas (42 Pixel je 512/12 = 42,67 Einträge).
    """
    import numpy as np
    from flash import spi_init, read as flash_read

    if length is None:
        length = SHADING_LEN[dpi]
    base, size = SHADING_FLASH[dpi]
    spi_init(dev)
    raw = flash_read(dev, base, size)

    hdr = np.frombuffer(raw[:32], dtype="<u4")
    pixels = int(hdr[6]) if len(hdr) > 6 else 0
    if not (pixels and int(hdr[5]) == pixels * 3):
        pixels = {300: 2550, 600: 5100}[dpi]

    src = raw[32:32 + pixels * 12]
    if len(src) < pixels * 12:
        raise RuntimeError("Kalibrierdaten im Flash unvollständig")

    step = SHADING_BLOCK_PX * 12
    out = b"".join(src[i:i + step].ljust(SHADING_BLOCK, b"\x00")
                   for i in range(0, len(src), step))
    return out.ljust(length, b"\x00")[:length]


if __name__ == "__main__":
    import sys
    from pathlib import Path
    ref = (pathlib.Path(__file__).resolve().parents[1] / "vendor/tables")

    def compare(name, made, path):
        want = (ref / path).read_bytes()
        a = struct.unpack(f"<{len(want)//2}H", want)
        b = struct.unpack(f"<{len(made)//2}H", made)
        diff = [abs(x - y) for x, y in zip(a, b)]
        rel = [d / max(1, x) * 100 for d, x in zip(diff, a)]
        print(f"{name:8s} gegen {path}: identisch={made == want}  "
              f"max. Abweichung {max(diff)} ({max(rel):.2f} %)")

    compare("gamma", gamma_table(), "dl04_01000000.bin")
    compare("slope", slope_table(), "dl01_10000000.bin")
    compare("slope*", slope_from_curve("20,3000,250"), "dl01_10000000.bin")
