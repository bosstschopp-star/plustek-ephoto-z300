#!/usr/bin/env python3
"""
SPI-Flash des ePhoto Z300 auslesen.

Der Flash enthält laut Vendor-Konfiguration die Shading-/Kalibrierdaten
(SaveShadingToFlash=1, flash_addr_sheetfed_300/600).

Protokoll rekonstruiert aus CIOUSB::SFlash_Read / CIOUSB::SFlash_RDID.  Beide
Funktionen verzweigen auf `this->[0x12] != 0x602` — ein Hardware-Revisionstest.
Dieses Gerät meldet bcdDevice 0x0702 und nimmt daher den einfachen Pfad, der
ohne den memory-mapped SPI-Controller auskommt:

    Adresse setzen: ControlWrite(wValue=0x8B, wIndex=0x0B, uint32_le(addr))
    Daten lesen:    ControlRead (wValue=0x8A, wIndex=0x0F)  in 0x40-Blöcken
    JEDEC-ID:       ControlRead (wValue=0x8A, wIndex=0x10, 3 Byte)

Rein lesend — der Adress-Schreibzugriff setzt lediglich einen Lesezeiger.
"""
import pathlib
import argparse
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from ephoto import EPhoto, DeviceError, REQ_IN, REQ_OUT, BREQUEST  # noqa: E402

VALUE_FLASH_CMD = 0x8B      # Control-OUT: Unteroperation über wIndex
INDEX_SPI_ENABLE = 0x07     #   SPI freischalten (1 Byte, 0x01)
INDEX_SPI_CONFIG = 0x09     #   Konfiguration    (4 Byte, 0)
INDEX_FLASH_ADDR = 0x0B     #   Leseadresse      (4 Byte)

VALUE_FLASH_DATA = 0x8A     # Control-IN
INDEX_FLASH_DATA = 0x0F     #   Datenstrom
INDEX_FLASH_ID = 0x10       #   JEDEC-ID (3 Byte)

CHUNK = 0x40
# Kurz halten: bleibt eine Antwort aus, soll der Endpoint nicht sekundenlang
# belegt bleiben — ein hängender Control-Transfer hat das Gerät schon einmal
# vom Bus genommen.
TIMEOUT = 700


def alive(dev):
    """Lebenszeichen: ein Register lesen, das immer antwortet."""
    try:
        dev.read_reg(0x03, timeout=TIMEOUT)
        return True
    except Exception:  # noqa: BLE001
        return False


def spi_init(dev):
    """Freischaltsequenz aus CIOUSB::SPI_init (Zweig bcdDevice != 0x602)."""
    dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_FLASH_CMD,
                          INDEX_SPI_ENABLE, bytes([0x01]), TIMEOUT)
    dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_FLASH_CMD,
                          INDEX_SPI_CONFIG, (0).to_bytes(4, "little"), TIMEOUT)

# Auszug aus der JEDEC-Herstellerliste, nur was bei Scannern üblich ist
JEDEC_VENDORS = {
    0x20: "Micron/ST", 0x1F: "Atmel/Adesto", 0xEF: "Winbond", 0xC2: "Macronix",
    0xBF: "SST", 0x01: "Spansion", 0x9D: "ISSI", 0xC8: "GigaDevice",
    0x8C: "ESMT", 0x0B: "XTX", 0x68: "Boya", 0x5E: "Zbit",
}


def read_id(dev):
    r = dev.dev.ctrl_transfer(REQ_IN, BREQUEST, VALUE_FLASH_DATA,
                              INDEX_FLASH_ID, 3, TIMEOUT)
    return bytes(r)


def read(dev, addr, length):
    """`length` Bytes ab `addr` lesen."""
    n = dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_FLASH_CMD,
                              INDEX_FLASH_ADDR,
                              addr.to_bytes(4, "little"), TIMEOUT)
    if n != 4:
        raise DeviceError(f"Adresse setzen fehlgeschlagen: {n}/4 Byte")

    out = bytearray()
    remaining = length
    while remaining > 0:
        want = min(remaining, CHUNK)
        got = dev.dev.ctrl_transfer(REQ_IN, BREQUEST, VALUE_FLASH_DATA,
                                    INDEX_FLASH_DATA, want, TIMEOUT)
        if not len(got):
            raise DeviceError(f"Leerer Block bei Offset {len(out):#x}")
        out += bytes(got)
        remaining -= len(got)
    return bytes(out[:length])


def hexdump(data, base=0, width=16, maxlines=None):
    lines = []
    for off in range(0, len(data), width):
        if maxlines and len(lines) >= maxlines:
            lines.append(f"  ... ({len(data) - off} weitere Bytes)")
            break
        chunk = data[off:off + width]
        hx = " ".join(f"{c:02x}" for c in chunk).ljust(width * 3 - 1)
        tx = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
        lines.append(f"  {base + off:06x}  {hx}  |{tx}|")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--length", type=lambda s: int(s, 0), default=0x100)
    ap.add_argument("--out", help="Rohdaten in Datei schreiben")
    ap.add_argument("--id-only", action="store_true")
    args = ap.parse_args()

    dev = EPhoto()
    if not alive(dev):
        sys.exit("Gerät antwortet nicht — bitte aus- und wieder einstecken.")

    print("SPI freischalten (SPI_init) ...")
    try:
        spi_init(dev)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"SPI_init fehlgeschlagen: {type(e).__name__}: {e}")
    if not alive(dev):
        sys.exit("Gerät nach SPI_init nicht mehr ansprechbar — bitte neu einstecken.")
    print("  OK, Gerät weiterhin ansprechbar\n")

    ident = read_id(dev)
    vendor = JEDEC_VENDORS.get(ident[0], "unbekannt")
    size = 1 << ident[2] if 0x10 <= ident[2] <= 0x1A else None
    print(f"JEDEC-ID: {ident.hex(' ')}")
    print(f"  Hersteller 0x{ident[0]:02x} ({vendor}), Typ 0x{ident[1]:02x}, "
          f"Kapazität 0x{ident[2]:02x}" + (f" = {size // 1024} KiB" if size else ""))

    if args.id_only:
        return
    if ident[0] in (0x00, 0xFF):
        print("\nUnplausible ID — vermutlich antwortet kein Flash. Abbruch.")
        return

    print(f"\nLese {args.length} Byte ab {args.addr:#x} ...")
    data = read(dev, args.addr, args.length)
    print(f"gelesen: {len(data)} Byte, "
          f"{len(set(data))} verschiedene Bytewerte\n")
    print(hexdump(data, args.addr, maxlines=32))

    if args.out:
        with open(args.out, "wb") as f:
            f.write(data)
        print(f"\ngeschrieben: {args.out}")


if __name__ == "__main__":
    main()
