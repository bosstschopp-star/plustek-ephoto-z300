#!/usr/bin/env python3
"""Locate and disassemble functions in the Plustek ScanApi Mach-O framework."""
import os
import pathlib
import sys, lief, capstone

# Das Hersteller-Binary ist nicht Teil des Repositorys.  Pfad per Umgebungs-
# variable angeben oder nach vendor/mac/ScanApi im Repository legen.
BIN = os.environ.get("EPHOTO_SCANAPI",
                     str(pathlib.Path(__file__).resolve().parents[2] / "vendor/mac/ScanApi"))


def load(arch="X86_64"):
    fat = lief.MachO.parse(BIN)
    for i in range(len(fat)):
        b = fat.at(i)
        if b is None:
            continue
        if str(b.header.cpu_type).split(".")[-1].upper() == arch.upper():
            return b
    return fat.at(0)


def symbols(bin_):
    """Return sorted list of (addr, name) for defined function symbols."""
    out = []
    for s in bin_.symbols:
        try:
            v = s.value
        except Exception:
            continue
        if v and s.name:
            out.append((v, s.name))
    out.sort()
    return out


def find(bin_, pattern):
    pat = pattern.lower()
    return [(a, n) for a, n in symbols(bin_) if pat in n.lower()]


def disas(bin_, addr, name="", limit=180):
    syms = symbols(bin_)
    # next symbol address bounds the function
    nxt = None
    for a, n in syms:
        if a > addr:
            nxt = a
            break
    size = min((nxt - addr) if nxt else 0x400, 0x2000)
    content = bin_.get_content_from_virtual_address(addr, size)
    data = bytes(content)
    is_arm = "ARM64" in str(bin_.header.cpu_type).upper()
    md = capstone.Cs(capstone.CS_ARCH_ARM64 if is_arm else capstone.CS_ARCH_X86,
                     capstone.CS_MODE_ARM if is_arm else capstone.CS_MODE_64)
    md.detail = False
    print(f"\n===== {name or hex(addr)}  @ {hex(addr)}  ({size} bytes) =====")
    for i, ins in enumerate(md.disasm(data, addr)):
        if i >= limit:
            print("   ... (truncated)")
            break
        print(f"  {ins.address:#x}:  {ins.mnemonic:<10} {ins.op_str}")


if __name__ == "__main__":
    b = load()
    print(f"# slice: {b.header.cpu_type}, symbols: {len(list(b.symbols))}")
    if len(sys.argv) > 1:
        q = sys.argv[1]
        hits = find(b, q)
        print(f"# matches for {q!r}: {len(hits)}")
        for a, n in hits[:40]:
            print(f"  {a:#x}  {n}")
        if len(sys.argv) > 2 and sys.argv[2] == "-d":
            for a, n in hits[: int(sys.argv[3]) if len(sys.argv) > 3 else 3]:
                disas(b, a, n)
