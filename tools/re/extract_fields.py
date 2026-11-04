#!/usr/bin/env python3
"""
Mehrbyte-Registerfelder aus den Chipklassen extrahieren.

Ergänzung zu extract_bits.py: dort werden nur Einzelbit-Felder erkannt
(Read-Modify-Write auf ein Byte).  Felder wie FEEDL oder LPERIOD erstrecken sich
über mehrere Register und folgen einem anderen Muster:

    CRegister::SetRegs(reg, wert, count, ...)
    CIOUSB::WriteRegs(reg, &shadow[reg*2], count, ...)

Registernummer steht als Immediate in esi, die Anzahl in ecx — beide unmittelbar
vor dem Aufruf.  Zusätzlich wird der Shadow-Offset (`add rdx, N`) als Gegenprobe
gelesen: N/2 muss gleich reg sein.
"""
import pathlib
import re
import subprocess
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from disas import load, symbols  # noqa: E402

CHIPS = ("125", "126", "128", "846")


def decode(mangled):
    m = re.match(r"^_?_ZN6CGL(\d{3})(\d+)([A-Za-z0-9_]+)E", mangled)
    if not m:
        return None
    chip, ln, rest = m.group(1), int(m.group(2)), m.group(3)
    return chip, rest[:ln]


def analyse(bin_, addr, size, targets):
    data = bytes(bin_.get_content_from_virtual_address(addr, size))
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    esi = ecx = off = None
    for i in md.disasm(data, addr):
        op = i.op_str
        if i.mnemonic == "mov":
            m = re.match(r"^esi, (0x[0-9a-f]+|\d+)$", op)
            if m:
                esi = int(m.group(1), 0)
            m = re.match(r"^ecx, (0x[0-9a-f]+|\d+)$", op)
            if m:
                ecx = int(m.group(1), 0)
        if i.mnemonic == "add":
            m = re.match(r"^rdx, (0x[0-9a-f]+|\d+)$", op)
            if m:
                off = int(m.group(1), 0)
        if i.mnemonic == "call" and op.startswith("0x"):
            if int(op, 16) in targets and esi is not None:
                return {"reg": esi, "count": ecx or 1, "offset": off}
    return None


def main():
    b = load()
    syms = symbols(b)
    by = {}
    for a, n in syms:
        by.setdefault(a, n)
    starts = sorted(by)

    targets = {a for a, n in syms
               if "CIOUSB" in n and re.search(r"9WriteRegsE|8ReadRegsE", n)}

    res = {c: {} for c in CHIPS}
    for idx, a in enumerate(starts):
        d = decode(by[a])
        if not d or d[0] not in res:
            continue
        chip, name = d
        size = min((starts[idx + 1] - a) if idx + 1 < len(starts) else 0x200, 0x300)
        if size < 16:
            continue
        r = analyse(b, a, size, targets)
        if r and r["count"] and r["count"] > 1:
            res[chip].setdefault(name, r)

    names = sorted(set().union(*(set(res[c]) for c in CHIPS)))
    print(f"# Mehrbyte-Felder: " + ", ".join(f"CGL{c}={len(res[c])}" for c in CHIPS))
    print(f"\n{'FELD':<12} " + " ".join(f"CGL{c:<9}" for c in CHIPS)
          + "   (Register .. Anzahl)")
    print("-" * 70)
    for n in names:
        cells = []
        for c in CHIPS:
            r = res[c].get(n)
            if r:
                hi = r["reg"] + r["count"] - 1
                cells.append(f"{r['reg']:#04x}-{hi:#04x}".ljust(12))
            else:
                cells.append("-".ljust(12))
        print(f"{n:<12} " + " ".join(cells))


if __name__ == "__main__":
    main()
