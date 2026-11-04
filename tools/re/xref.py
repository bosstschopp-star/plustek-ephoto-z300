#!/usr/bin/env python3
"""Find call/reference sites for given symbols in the ScanApi Mach-O slice."""
import pathlib
import sys, re, capstone
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from disas import load, symbols, disas  # noqa: E402


def build(bin_):
    syms = symbols(bin_)
    by_addr = {}
    for a, n in syms:
        by_addr.setdefault(a, n)
    return syms, by_addr


def text_range(bin_):
    for s in bin_.sections:
        if s.name == "__text":
            return s.virtual_address, s.size
    raise SystemExit("no __text")


def scan(bin_, targets):
    """targets: set of addresses. Returns {target: [(site_addr, site_sym)]}."""
    syms, by_addr = build(bin_)
    base, size = text_range(bin_)
    data = bytes(bin_.get_content_from_virtual_address(base, size))
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = False

    # sorted symbol starts for attributing a site to its enclosing function
    starts = [a for a, _ in syms]
    starts.sort()

    def enclosing(addr):
        lo, hi = 0, len(starts) - 1
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            if starts[mid] <= addr:
                best = starts[mid]
                lo = mid + 1
            else:
                hi = mid - 1
        return best, by_addr.get(best, "?")

    # Linear disassembly desyncs on padding/data, so scan for the raw encodings
    # of rip-relative control flow instead:
    #   E8 rel32  = call    E9 rel32 = jmp
    #   48 8D 05 rel32 = lea rax, [rip+disp]   (vtable / object refs)
    import struct
    hits = {t: [] for t in targets}

    def note(site, tgt):
        if tgt in hits:
            _, fn = enclosing(site)
            hits[tgt].append((site, fn))

    n = len(data)
    for i in range(n - 5):
        op = data[i]
        if op in (0xE8, 0xE9):
            rel = struct.unpack_from("<i", data, i + 1)[0]
            note(base + i, base + i + 5 + rel)
        elif op == 0x48 and i + 7 <= n and data[i + 1] == 0x8D:
            modrm = data[i + 2]
            if (modrm & 0xC7) == 0x05:  # rip-relative
                rel = struct.unpack_from("<i", data, i + 3)[0]
                note(base + i, base + i + 7 + rel)
    return hits


if __name__ == "__main__":
    b = load()
    pat = sys.argv[1]
    syms, by_addr = build(b)
    targets = {a: n for a, n in syms if pat in n}
    print(f"# targets matching {pat!r}: {len(targets)}")
    for a, n in list(targets.items())[:20]:
        print(f"   {a:#x} {n}")
    hits = scan(b, set(targets))
    print("\n# reference sites:")
    for t, lst in hits.items():
        if not lst:
            continue
        print(f"\n-> {targets[t]} ({t:#x}):")
        seen = set()
        for addr, fn in lst:
            key = fn or hex(addr)
            if key in seen:
                continue
            seen.add(key)
            print(f"     {addr:#x}  in  {fn or '<unnamed>'}")
