#!/usr/bin/env python3
"""
Eine Registersequenz aus einem `regwrites`-Textprotokoll in Schritte überführen.

Für 300 dpi stammte die Sequenz aus dem USB-Mitschnitt (tools/extract_sequence.py).
Für 600 dpi liegt sie als Textprotokoll vor — gleiches Ergebnis, andere Quelle.

Zeilenformen:
    123 W        [32] 01=22 02=78 ...        Register schreiben
    124 Wc       code=0x8c idx=0x0010 data=0d Kommando
    125 BUFSET   idx=0x0001 data=<16 hex>     Adresse+Länge, idx 1 = schreiben,
                                              8/0 = lesen
    126 BULK_OUT ...                          Nutzdaten (hier ignoriert, die
                                              Tabellen werden erzeugt)
"""
import re
import struct
import sys

DL_BY_ADDR = {0x10014000: "shading", 0x10000000: "slope0", 0x10004000: "slope1",
              0x10008000: "slope2", 0x01000000: "gamma_r",
              0x01000200: "gamma_g", 0x01000400: "gamma_b"}


def parse(path):
    steps = []
    for line in open(path, encoding="utf-8-sig", errors="replace"):
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^\d+\s+(\S+)\s+(.*)$", s)
        if not m:
            continue
        kind, rest = m.group(1), m.group(2)

        if kind == "W":
            pairs = re.findall(r"\b([0-9a-fA-F]{2})=([0-9a-fA-F]{2})\b", rest)
            if pairs:
                steps.append(("W", "".join(a + b for a, b in pairs)))
        elif kind == "Wc":
            c = re.search(r"code=0x([0-9a-fA-F]+)", rest)
            i = re.search(r"idx=0x([0-9a-fA-F]+)", rest)
            d = re.search(r"data=([0-9a-fA-F]+)", rest)
            if c and i:
                steps.append(("C", int(c.group(1), 16), int(i.group(1), 16),
                              d.group(1) if d else ""))
        elif kind == "BUFSET":
            i = re.search(r"idx=0x([0-9a-fA-F]+)", rest)
            d = re.search(r"data=([0-9a-fA-F]{16})", rest)
            if not (i and d):
                continue
            idx = int(i.group(1), 16)
            addr, length = struct.unpack("<II", bytes.fromhex(d.group(1)))
            if idx == 1:
                name = DL_BY_ADDR.get(addr)
                if name:
                    steps.append(("D", name, addr, length))
            else:
                steps.append(("R", addr, length, idx))
    return steps


if __name__ == "__main__":
    st = parse(sys.argv[1])
    kinds = {}
    for s in st:
        kinds[s[0]] = kinds.get(s[0], 0) + 1
    print(f"{len(st)} Schritte: {kinds}")
    reads = [s for s in st if s[0] == "R"]
    total = sum(s[2] for s in reads)
    for s in st:
        if s[0] == "D":
            print(f"  Download {s[1]:8s} {s[2]:#010x}  {s[3]} B")
    if reads:
        print(f"  Lesen: {len(reads)} Blöcke, {total} Byte")
        sizes = sorted({s[2] for s in reads})
        print(f"    Blockgrößen: {sizes}")
        for stride in (7650, 15300):
            if total % stride == 0:
                print(f"    -> {total//stride} Zeilen bei Stride {stride}")
