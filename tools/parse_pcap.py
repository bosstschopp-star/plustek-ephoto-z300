#!/usr/bin/env python3
"""
Minimaler USBPcap-Leser: Control-Setup-Pakete herausziehen.

Zweck hier: die tatsächlichen bmRequestType/bRequest-Werte aus einem echten
Windows-Scan bestätigen. Die Übergabedoku nennt bRequest 0x04/0x0c, die aus dem
macOS-Framework abgeleitete und am Gerät funktionierende Implementierung
verwendet 0xA0 — das gehört geprüft, nicht geglaubt.

USBPcap-Paketkopf (LINKTYPE_USBPCAP = 249), little endian:
   0  u16 headerLen
   2  u64 irpId
  10  u32 status
  14  u16 function
  16  u8  info
  17  u16 bus
  19  u16 device
  21  u8  endpoint
  22  u8  transfer      (0 = isoch, 1 = interrupt, 2 = control, 3 = bulk)
  23  u32 dataLength
  27  ... bei Control: 8 Byte Setup (bmRequestType, bRequest, wValue,
      wIndex, wLength), sofern headerLen >= 27+8
"""
import struct
import sys
from collections import Counter


def packets(path):
    with open(path, "rb") as f:
        gh = f.read(24)
        magic, = struct.unpack_from("<I", gh, 0)
        if magic == 0xA1B2C3D4:
            endian = "<"
        elif magic == 0xD4C3B2A1:
            endian = ">"
        else:
            sys.exit(f"kein klassisches pcap (magic {magic:#x})")
        link, = struct.unpack_from(endian + "I", gh, 20)
        while True:
            ph = f.read(16)
            if len(ph) < 16:
                return
            ts, tus, caplen, origlen = struct.unpack(endian + "IIII", ph)
            data = f.read(caplen)
            if len(data) < caplen:
                return
            yield ts + tus / 1e6, data


def control_setups(path):
    for t, d in packets(path):
        if len(d) < 27:
            continue
        hlen = struct.unpack_from("<H", d, 0)[0]
        transfer = d[22]
        if transfer != 2:               # nur Control
            continue
        # Das Setup-Paket liegt AM Kopfende (Offset = headerLen), 8 Byte.
        if len(d) < hlen + 8:
            continue
        bm, br, wval, widx, wlen = struct.unpack_from("<BBHHH", d, hlen)
        payload = d[hlen + 8:hlen + 8 + wlen]
        yield t, bm, br, wval, widx, wlen, payload


def main():
    path = sys.argv[1]
    n = 0
    combos = Counter()
    first = []
    for t, bm, br, wval, widx, wlen, payload in control_setups(path):
        n += 1
        combos[(bm, br, wval)] += 1
        if len(first) < 14:
            first.append((bm, br, wval, widx, wlen, payload[:16].hex()))

    print(f"# Control-Transfers: {n}\n")
    print("bmReqType bRequest wValue    Anzahl")
    print("-" * 40)
    for (bm, br, wval), c in combos.most_common(20):
        print(f"  {bm:#04x}     {br:#04x}    {wval:#06x}   {c}")

    print("\n# erste Transfers:")
    for bm, br, wval, widx, wlen, hx in first:
        print(f"  bm={bm:#04x} bReq={br:#04x} wValue={wval:#06x} "
              f"wIndex={widx:#06x} wLen={wlen:<5} {hx}")


if __name__ == "__main__":
    main()
