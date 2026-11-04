#!/usr/bin/env python3
"""Aufrufkette einer Funktion auflisten — die Struktur ohne den Registerlärm."""
import pathlib
import re
import sys

import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from disas import load, symbols  # noqa: E402


def demangle_all(names):
    """Namen gesammelt durch c++filt schicken (Mach-O trägt ein führendes _)."""
    import subprocess
    stripped = [n[1:] if n.startswith("__Z") else n for n in names]
    try:
        p = subprocess.run(["c++filt"], input="\n".join(stripped),
                           capture_output=True, text=True, timeout=30)
        out = p.stdout.splitlines()
        if len(out) == len(names):
            return dict(zip(names, out))
    except Exception:  # noqa: BLE001
        pass
    return {n: n for n in names}


def main():
    target = sys.argv[1]
    span = int(sys.argv[2], 0) if len(sys.argv) > 2 else 0x600

    b = load()
    syms = symbols(b)
    by = {}
    for a, n in syms:
        by.setdefault(a, n)

    hits = [a for a, n in syms if target in n]
    if not hits:
        sys.exit(f"kein Symbol mit {target!r}")
    addr = sorted(set(hits))[0]
    print(f"# {by[addr]} @ {addr:#x}\n")

    data = bytes(b.get_content_from_virtual_address(addr, span))
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)

    seq = []
    for i in md.disasm(data, addr):
        if i.mnemonic == "ret":
            break
        if i.mnemonic == "call":
            if i.op_str.startswith("0x"):
                seq.append((i.address, by.get(int(i.op_str, 16)),
                            int(i.op_str, 16)))
            else:
                seq.append((i.address, None, None))

    names = sorted({n for _, n, _ in seq if n})
    dm = demangle_all(names)
    for a, n, t in seq:
        if n:
            label = dm.get(n, n)
        elif t:
            label = f"sub_{t:x}"
        else:
            label = "[virtueller Aufruf]"
        print(f"  {a:#08x}  {label}")
    print(f"\n# {len(seq)} Aufrufe")


if __name__ == "__main__":
    main()
