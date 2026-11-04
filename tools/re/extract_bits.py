#!/usr/bin/env python3
"""
Extract the register-bit map from the Plustek ScanApi framework.

Each chip class (CGL125/126/128/846) implements one method per named register
bit.  The generated code follows a fixed shape:

    mov  rax, [this + 0x10]        ; shadow register image (uint16 per register)
    mov  cx,  word [rax + OFF]     ; OFF = reg * 2
    and  dx,  MASKBITS             ; isolate the incoming value
    shl  dx,  SHIFT                ; move it into position
    and  cx,  ~(BITS << SHIFT)     ; clear the field
    or   cx,  dx
    mov  word [rax + OFF], cx
    ...
    mov  esi, REG                  ; <- register number, passed to WriteRegs
    call CIOUSB::WriteRegs

We recover (register, mask) from the `and cx, IMM` clear-mask plus the `mov esi,
REG` immediately preceding the WriteRegs call.  Both are cross-checked against
each other: reg*2 must equal the shadow offset.
"""
import pathlib
import re
import sys
import capstone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from disas import load, symbols  # noqa: E402

CHIPS = ("125", "126", "128", "846")
WRITE_FNS = ("WriteRegs", "WriteReg")


def decode_name(mangled):
    """__ZN6CGL8467LAMPPWREibi -> ('846', 'LAMPPWR')"""
    m = re.match(r"^_?_ZN6CGL(\d{3})(\d+)([A-Za-z0-9_]+)E", mangled)
    if not m:
        return None
    chip, ln, rest = m.group(1), int(m.group(2)), m.group(3)
    return chip, rest[:ln]


def analyse(bin_, addr, size, write_targets):
    """Return dict(reg=, mask=, shift=, offset=) or None."""
    data = bytes(bin_.get_content_from_virtual_address(addr, size))
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = False
    ins = list(md.disasm(data, addr))

    offset = clear_mask = shift = reg = None
    last_esi = None
    for i in ins:
        op = i.op_str
        # shadow-image field access:  mov cx, word ptr [rax + 0x6]
        m = re.match(r"^(?:e?[a-d]x|[a-d][xl]), (?:word|byte) ptr \[rax \+ (0x[0-9a-f]+)\]$", op)
        if m and offset is None:
            offset = int(m.group(1), 16)
        elif re.match(r"^(?:e?[a-d]x|[a-d][xl]), (?:word|byte) ptr \[rax\]$", op) and offset is None:
            offset = 0
        # clear mask:  and cx, 0xffef   /  and cl, 0xef
        m = re.match(r"^([a-d][xl]), (0x[0-9a-f]+)$", op)
        if m and i.mnemonic == "and" and clear_mask is None:
            v = int(m.group(2), 16)
            if v in (0xFFEF, 0xEF) or (v & 0xFF) not in (0x00, 0x01, 0x03, 0x07, 0x0F):
                clear_mask = v
        # shift amount:  shl dx, 4
        m = re.match(r"^[a-d][xl], (\d+)$", op)
        if m and i.mnemonic == "shl" and shift is None:
            shift = int(m.group(1))
        # register number handed to WriteRegs
        m = re.match(r"^esi, (0x[0-9a-f]+|\d+)$", op)
        if m and i.mnemonic == "mov":
            last_esi = int(m.group(1), 0)
        if i.mnemonic == "call" and op.startswith("0x"):
            if int(op, 16) in write_targets and last_esi is not None:
                reg = last_esi
                break

    if reg is None and offset is not None:
        reg = offset // 2  # uint16 shadow image
    if reg is None:
        return None

    # derive the set-mask from the clear-mask (bits that get zeroed)
    mask = None
    if clear_mask is not None:
        mask = (~clear_mask) & 0xFF
    elif shift is not None:
        mask = 1 << shift

    return {"reg": reg, "mask": mask, "shift": shift, "offset": offset}


def main():
    b = load()
    syms = symbols(b)
    by_addr = {}
    for a, n in syms:
        by_addr.setdefault(a, n)
    starts = sorted(by_addr)

    write_targets = {a for a, n in syms
                     if any(f"{len(f)}{f}E" in n for f in WRITE_FNS) and "CIOUSB" in n}

    results = {c: {} for c in CHIPS}
    for a in starts:
        n = by_addr[a]
        d = decode_name(n)
        if not d:
            continue
        chip, name = d
        if chip not in results:
            continue
        i = starts.index(a)
        size = min((starts[i + 1] - a) if i + 1 < len(starts) else 0x200, 0x400)
        if size < 16:
            continue
        r = analyse(b, a, size, write_targets)
        if r and r["mask"]:
            # keep the first (bool,int) overload we see per name
            results[chip].setdefault(name, r)

    ref = "846"
    names = sorted(results[ref])
    print(f"# extrahierte Bitdefinitionen: " +
          ", ".join(f"CGL{c}={len(results[c])}" for c in CHIPS))
    print(f"\n{'BIT':<14} " + " ".join(f"CGL{c:<9}" for c in CHIPS))
    print("-" * 62)
    agree = differ = 0
    for name in names:
        cells = []
        vals = set()
        for c in CHIPS:
            r = results[c].get(name)
            if r:
                cells.append(f"{r['reg']:#04x}/{r['mask']:#04x}".ljust(12))
                vals.add((r["reg"], r["mask"]))
            else:
                cells.append("-".ljust(12))
        if len(vals) == 1:
            agree += 1
        else:
            differ += 1
        print(f"{name:<14} " + " ".join(cells))
    print(f"\n# identisch über alle Chips: {agree}   abweichend: {differ}")


if __name__ == "__main__":
    main()
