#!/usr/bin/env python3
"""
Bulk-Datenpfad erproben — vorsichtig.

Rekonstruiert aus CIOUSB::ReadTransaction / StartReadTransaction /
SetupBulkLength.  Für bcdDevice >= 0x600 (dieses Gerät: 0x702) reduziert sich
der Start einer Leseoperation auf einen einzigen Control-Transfer:

    StartReadTransaction(len, addr)  ->  SetupBulkLength(addr, len, 0)
        ControlWrite(wValue=0x82, wIndex=0, u32(addr) + u32(len), len=8)
    ReadRawData(buf, &len)           ->  BulkRead auf EP 0x81
    EndReadTransaction(0)            ->  ControlWrite(wValue=0x8D, ..., [0x00], len=1)

`SetRegAddr` und `SetBulkMode` entfallen bei dieser Geräterevision.

Vorsichtsmassnahmen: kurze Timeouts, nur ein Paket, Lebenszeichen vor und nach
jedem Schritt, und bei Fehlschlag ein Versuch, den Endpoint zu entstopfen.
"""
import pathlib
import argparse
import sys

import usb.core

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from ephoto import EPhoto, REQ_IN, REQ_OUT, BREQUEST  # noqa: E402

VALUE_BUFFER = 0x82         # entspricht VALUE_BUFFER im SANE-genesys-Backend
VALUE_END_TRANSACTION = 0x8D
EP_BULK_IN = 0x81
EP_BULK_OUT = 0x02

CTRL_TIMEOUT = 700
BULK_TIMEOUT = 300          # bewusst kurz: ein hängender Bulk-Read hat das
                            # Gerät schon einmal vom USB-Bus genommen


def alive(dev):
    try:
        dev.read_reg(0x03, timeout=CTRL_TIMEOUT)
        return True
    except Exception:  # noqa: BLE001
        return False


# --- Speicherkarte des CGL126 (aus CGL126::MotorAddr/ImageAddr/... ) --------
MOTOR_TABLE = (0x10000000, 0x10004000, 0x10008000, 0x1000C000, 0x10010000)
SHADING_ADDR = 0x10014000
GAMMA_ADDR = (0x01000000, 0x01000200, 0x01000400)


def _setup(dev, addr, length, direction):
    """SetupBulkLength(addr, len, flag) — flag 0 = lesen, 1 = schreiben."""
    payload = addr.to_bytes(4, "little") + length.to_bytes(4, "little")
    n = dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_BUFFER, direction,
                              payload, CTRL_TIMEOUT)
    if n != 8:
        raise RuntimeError(f"SetupBulkLength: {n}/8 Byte")


def start_read(dev, addr, length):
    _setup(dev, addr, length, 0)


def read_block(dev, addr, length, timeout=BULK_TIMEOUT):
    start_read(dev, addr, length)
    data = bytes(dev.dev.read(EP_BULK_IN, length, timeout))
    end_read(dev)
    return data


def write_block(dev, addr, data, timeout=BULK_TIMEOUT):
    """WriteSramData für bcdDevice >= 0x600: Setup mit Flag 1, dann Bulk-OUT."""
    _setup(dev, addr, len(data), 1)
    n = dev.dev.write(EP_BULK_OUT, data, timeout)
    if n != len(data):
        raise RuntimeError(f"BulkWrite: {n}/{len(data)} Byte")
    return n


def end_read(dev):
    dev.dev.ctrl_transfer(REQ_OUT, BREQUEST, VALUE_END_TRANSACTION, 0,
                          bytes([0x00]), CTRL_TIMEOUT)


def unstick(dev):
    """Nach einem fehlgeschlagenen Bulk-Transfer den Endpoint freiräumen."""
    for ep in (EP_BULK_IN, EP_BULK_OUT):
        try:
            dev.dev.clear_halt(ep)
        except Exception:  # noqa: BLE001
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--addr", type=lambda s: int(s, 0), default=0)
    ap.add_argument("--length", type=lambda s: int(s, 0), default=512)
    args = ap.parse_args()

    dev = EPhoto()
    if not alive(dev):
        sys.exit("Gerät antwortet nicht — bitte aus- und wieder einstecken.")
    print("Gerät ansprechbar.\n")

    print(f"1) StartReadTransaction(len={args.length}, addr={args.addr:#x})")
    print(f"   ControlWrite(0x82, wIndex=0, "
          f"{args.addr.to_bytes(4,'little').hex()} "
          f"{args.length.to_bytes(4,'little').hex()}, len=8)")
    try:
        start_read(dev, args.addr, args.length)
    except Exception as e:  # noqa: BLE001
        print(f"   FEHLGESCHLAGEN: {type(e).__name__}: {e}")
        print(f"   Gerät noch ansprechbar: {alive(dev)}")
        return
    print("   OK")
    if not alive(dev):
        sys.exit("   Gerät nach dem Setup nicht mehr ansprechbar — neu einstecken.")
    print("   Gerät weiterhin ansprechbar\n")

    print(f"2) BulkRead auf EP {EP_BULK_IN:#04x}, {args.length} Byte, "
          f"Timeout {BULK_TIMEOUT} ms")
    data = None
    try:
        data = bytes(dev.dev.read(EP_BULK_IN, args.length, BULK_TIMEOUT))
        print(f"   OK — {len(data)} Byte gelesen")
    except usb.core.USBTimeoutError:
        print("   TIMEOUT — an dieser Adresse stehen keine Daten bereit.")
        unstick(dev)
    except usb.core.USBError as e:
        print(f"   USB-Fehler: {e}")
        unstick(dev)

    print(f"   Gerät noch ansprechbar: {alive(dev)}\n")

    print("3) EndReadTransaction")
    try:
        end_read(dev)
        print("   OK")
    except Exception as e:  # noqa: BLE001
        print(f"   FEHLGESCHLAGEN: {type(e).__name__}: {e}")

    print(f"\nGerät am Ende ansprechbar: {alive(dev)}")

    if data:
        uniq = len(set(data))
        print(f"\nDaten: {len(data)} Byte, {uniq} verschiedene Bytewerte")
        for off in range(0, min(len(data), 128), 16):
            chunk = data[off:off + 16]
            hx = " ".join(f"{c:02x}" for c in chunk)
            tx = "".join(chr(c) if 32 <= c < 127 else "." for c in chunk)
            print(f"  {off:04x}  {hx:<47}  |{tx}|")


if __name__ == "__main__":
    main()
