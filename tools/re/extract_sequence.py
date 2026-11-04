#!/usr/bin/env python3
"""
Die vollständige Scan-Befehlssequenz aus dem USBPcap-Mitschnitt extrahieren.

Ergebnis: eine JSON-Datei, die sich 1:1 gegen das Gerät abspielen lässt.
Registerlesevorgänge (wValue 0x8e/0x8a) werden weggelassen — sie sind reine
Statusabfragen und für die Wiedergabe nicht nötig.

Schritttypen:
    {"op":"wreg",   "pairs":[[reg,val],...]}          wValue 0x83
    {"op":"cmd",    "value":..., "index":..., "data":"hex"}   wValue 0x8b/0x8c/0x8d
    {"op":"dl",     "addr":..., "len":..., "blob":"<datei>"}  BUFSET wIndex 1 + Bulk-OUT
    {"op":"read",   "addr":..., "len":..., "index":...}       BUFSET wIndex 8/0 + Bulk-IN
"""
import pathlib
import json
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from parse_pcap import packets  # noqa: E402


def main():
    src = sys.argv[1]
    dst = sys.argv[2]

    ctrl, bulk_out = [], bytearray()
    for t, d in packets(src):
        if len(d) < 27:
            continue
        hlen = struct.unpack_from("<H", d, 0)[0]
        transfer, ep = d[22], d[21]
        dl = struct.unpack_from("<I", d, 23)[0]
        if transfer == 2 and len(d) >= hlen + 8:
            bm, br, wval, widx, wlen = struct.unpack_from("<BBHHH", d, hlen)
            payload = bytes(d[hlen + 8:hlen + 8 + wlen])
            ctrl.append((t, bm, br, wval, widx, wlen, payload))
        elif transfer == 3 and not (ep & 0x80):
            bulk_out += d[hlen:hlen + dl]

    # Downloadblöcke aus dem Bulk-OUT-Strom in Reihenfolge zuschneiden
    dl_sizes = [(w, a, l) for (_, bm, br, wv, w, _, pl) in ctrl
                if bm == 0x40 and (wv & 0xFF) == 0x82 and len(pl) >= 8
                for a, l in [struct.unpack("<II", pl[:8])] if w == 1]
    total_dl = sum(l for _, _, l in dl_sizes)
    start = len(bulk_out) - total_dl
    if start < 0:
        sys.exit(f"Bulk-OUT zu kurz: {len(bulk_out)} < {total_dl}")
    print(f"Bulk-OUT {len(bulk_out)} Byte, Downloads {total_dl} Byte, "
          f"Versatz {start}")

    steps, off, ndl = [], start, 0
    for t, bm, br, wval, widx, wlen, pl in ctrl:
        lo = wval & 0xFF
        if bm == 0x40 and lo == 0x83:
            pairs = [[pl[i], pl[i + 1]] for i in range(0, len(pl) - 1, 2)]
            if pairs:
                steps.append({"op": "wreg", "pairs": pairs})
        elif bm == 0x40 and lo in (0x8B, 0x8C, 0x8D):
            steps.append({"op": "cmd", "value": wval, "index": widx,
                          "data": pl.hex()})
        elif bm == 0x40 and lo == 0x82 and len(pl) >= 8:
            addr, ln = struct.unpack("<II", pl[:8])
            if widx == 1:
                name = f"dl{ndl:02d}_{addr:08x}.bin"
                open(pathlib.Path(__file__).resolve().parents[2] / "vendor/tables" / name,
                     "wb").write(bytes(bulk_out[off:off + ln]))
                steps.append({"op": "dl", "addr": addr, "len": ln, "blob": name})
                off += ln
                ndl += 1
            else:
                steps.append({"op": "read", "addr": addr, "len": ln,
                              "index": widx})

    json.dump(steps, open(dst, "w"), indent=1)
    kinds = {}
    for s in steps:
        kinds[s["op"]] = kinds.get(s["op"], 0) + 1
    print(f"{len(steps)} Schritte -> {dst}")
    print("  ", kinds)
    rd = sum(s["len"] for s in steps if s["op"] == "read")
    print(f"   Bilddaten insgesamt: {rd} Byte = {rd/7650:.1f} Zeilen")


if __name__ == "__main__":
    main()
