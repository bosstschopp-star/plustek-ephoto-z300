#!/usr/bin/env python3
"""
Full read-only dump of the ePhoto Z300 register space (0x000-0x1FF).

Reads every register twice to separate stable registers from volatile ones
(counters, status bits) and from unimplemented addresses.  Read-only: nothing
is ever written.
"""
import pathlib
import sys
import usb.core

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from probe import open_device, read_reg  # noqa: E402


def sweep(dev, lo, hi):
    """Return {reg: (pass1_bytes, pass2_bytes)}, None where the read failed."""
    first = {}
    for reg in range(lo, hi + 1):
        try:
            first[reg] = read_reg(dev, reg)
        except usb.core.USBError:
            first[reg] = None
    second = {}
    for reg in range(lo, hi + 1):
        try:
            second[reg] = read_reg(dev, reg)
        except usb.core.USBError:
            second[reg] = None
    return {r: (first[r], second[r]) for r in first}


def main():
    dev = open_device()
    lo, hi = 0x000, 0x1FF
    res = sweep(dev, lo, hi)

    ok = [r for r, (a, b) in res.items() if a is not None]
    failed = [r for r, (a, b) in res.items() if a is None]
    volatile = [r for r, (a, b) in res.items() if a is not None and a != b]

    print(f"# gelesen: {len(ok)}/{hi - lo + 1}   Fehler: {len(failed)}   "
          f"instabil: {len(volatile)}\n")

    # byte 0 of the 2-byte reply, second pass
    print("       " + " ".join(f"{c:02x}" for c in range(16)))
    for base in range(lo, hi + 1, 16):
        cells = []
        for reg in range(base, base + 16):
            a, b = res.get(reg, (None, None))
            cells.append("--" if b is None else b[0:1].hex())
        print(f" {base:04x}: " + " ".join(cells))

    # is the second byte ever non-zero?  tells us whether the reply is really
    # 2 meaningful bytes or just one value plus padding
    nz2 = {r: b.hex() for r, (a, b) in res.items() if b is not None and len(b) > 1 and b[1]}
    print(f"\n# Register mit Byte[1] != 0: {len(nz2)}")
    for r, h in list(nz2.items())[:24]:
        print(f"   {r:#05x}: {h}")

    if volatile:
        print(f"\n# instabile Register (Statusbits/Zähler):")
        for r in volatile[:32]:
            a, b = res[r]
            print(f"   {r:#05x}: {a.hex()} -> {b.hex()}")


if __name__ == "__main__":
    main()
