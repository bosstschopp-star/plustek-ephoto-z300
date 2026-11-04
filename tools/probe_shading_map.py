#!/usr/bin/env python3
"""
Die Zuordnung Kalibriereintrag -> Bildspalte direkt am Gerät ausmessen.

Statt zu raten, wie der Chip die 2590 Kalibriereinträge auf die 2550 Bildpixel
abbildet: eine Markierung in die Kalibrierung schreiben und nachsehen, wo sie
im Bild landet.

Vorgehen: Shading mit neutraler Kennlinie füllen (Dunkel 0, Gain 0x4000), an
wenigen bekannten Positionen die Verstärkung halbieren, scannen — die
resultierenden dunklen Spalten zeigen die Zuordnung.

Braucht eine gleichmässig helle Vorlage im Einzug.
"""
import sys
import struct
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from driver import open_device, scan, paper_present, GEOMETRY  # noqa: E402
from tables import gamma_table, slope_for  # noqa: E402

MARKS = [100, 500, 1000, 1500, 2000, 2400]      # Einträge, die markiert werden
UNITY, MARKED = 0x4000, 0x1000                  # normale bzw. gedämpfte Verstärkung


def build_marked(length, marks=MARKS):
    n = length // 2
    vals = [0, UNITY] * (n // 2)
    for m in marks:
        for ch in range(3):                     # alle Kanäle desselben Pixels
            i = (m * 3 + ch) * 2 + 1
            if i < n:
                vals[i] = MARKED
    return struct.pack(f"<{n}H", *vals)


def main():
    dpi = 300
    g = GEOMETRY[dpi]
    dev = open_device()
    if not paper_present(dev):
        sys.exit("Bitte eine gleichmässig helle Vorlage einlegen.")

    slope, gam = slope_for(dpi), gamma_table()
    tables = {"slope0": slope, "slope1": slope, "slope2": slope,
              "gamma_r": gam, "gamma_g": gam, "gamma_b": gam,
              "shading": build_marked(g["shading"])}
    print(f"Markierungen bei Kalibriereintrag: {MARKS}")

    raw = scan(dev, tables, dpi=dpi)
    n = len(raw) // g["stride"]
    img = np.frombuffer(raw[:n * g["stride"]], dtype=np.uint8)
    img = img.reshape(n, g["pixels"], 3).astype(np.float32)

    prof = img[int(n * 0.35):int(n * 0.65)].mean(axis=(0, 2))
    med = np.median(prof)
    dips = np.where(prof < med * 0.75)[0]

    # zusammenhängende Bereiche gruppieren
    groups, cur = [], []
    for c in dips:
        if cur and c - cur[-1] > 3:
            groups.append(cur)
            cur = []
        cur.append(int(c))
    if cur:
        groups.append(cur)

    print(f"\ngefundene dunkle Spalten: {len(groups)} Gruppen")
    for gr in groups:
        mid = sum(gr) // len(gr)
        near = min(MARKS, key=lambda m: abs(m - mid))
        print(f"  Bildspalte {gr[0]}..{gr[-1]} (Mitte {mid})   "
              f"nächste Markierung {near}   Versatz {mid - near:+d}")


if __name__ == "__main__":
    main()
