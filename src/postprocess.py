#!/usr/bin/env python3
"""
Nachbearbeitung des rohen Bilddatenstroms.

Befund aus dem ersten eigenen Scan (`captures/replay_raw.bin`):

Der Bulk-Strom enthält je Zeile Nullbyte-Paare an festen Offsets
88/89, 214/215, 340/341, … — Periode 126 Byte = 42 Pixel à 3 Kanäle.
Es sind Grün und Blau des Pixels p ≡ 29 (mod 42); Rot ist intakt.
Das sind die Sensorsegment-Grenzen.

Das Vendor-Referenzbild (`scan_CLEAN.png`) ist ebenfalls 2550 Pixel breit und
zeigt dort keine Artefakte — der Hersteller entfernt die Spalten also nicht,
sondern füllt sie. Genau das macht `fill_segment_gaps`.
"""
import pathlib
import argparse
import sys

import numpy as np
from PIL import Image

PIXELS, BYTES_PER_LINE = 2550, 7650

# Segmentgrenzen: Grün und Blau des Pixels p sind null, Periode in Pixeln.
# Nur bei 300 dpi vorhanden.  Bei 600 dpi liefert der Sensor lückenlos — am
# eigenen Scan gemessen (keine periodisch dunklen Spalten, keine Nullbytes
# ausser am rechten Rand).  Ein Fuellen wuerde dort echte Pixel ueberschreiben.
GAPS = {2550: (29, 42)}
GAP_FIRST, GAP_PERIOD = GAPS[2550]

# Die drei Farbzeilen des CIS sitzen räumlich versetzt.  Durch Rastersuche über
# ein Farbsaum-Mass am eigenen Scan bestimmt: R -6, B +6 Zeilen senkt den Saum
# von 23,7 auf 3,65 (-85 %).  Passt zu INI LINESPACE = -24 bei halber Auflösung
# (300 statt 600 dpi).
# Versatz der drei CIS-Farbzeilen, je Auflösung am eigenen Scan gemessen.
CHANNEL_LINE_OFFSET = (-6, 0, +6)
CHANNEL_OFFSETS = {2550: (-6, 0, +6), 5100: (-12, 0, +12)}


def load_raw(path, pixels=PIXELS):
    d = np.fromfile(path, dtype=np.uint8)
    lines = len(d) // (pixels * 3)
    return d[:lines * pixels * 3].reshape(lines, pixels, 3)


def gap_columns(width=PIXELS):
    if width not in GAPS:
        return np.empty(0, dtype=int)
    first, period = GAPS[width]
    return np.arange(first, width, period)


def fill_segment_gaps(img):
    """Grün/Blau der Segmentgrenz-Pixel aus den Nachbarn interpolieren."""
    out = img.copy()
    cols = gap_columns(img.shape[1])
    for c in cols:
        lo, hi = c - 1, c + 1
        if hi >= img.shape[1]:
            out[:, c, 1:] = img[:, lo, 1:]
        elif lo < 0:
            out[:, c, 1:] = img[:, hi, 1:]
        else:
            out[:, c, 1:] = ((img[:, lo, 1:].astype(np.uint16)
                              + img[:, hi, 1:].astype(np.uint16)) // 2)
    return out


def align_channels(img, offsets=None):
    if offsets is None:
        offsets = CHANNEL_OFFSETS.get(img.shape[1], CHANNEL_LINE_OFFSET)
    """Vertikalen Versatz der drei Sensorzeilen ausgleichen."""
    out = img.copy()
    for ch, off in enumerate(offsets):
        if off:
            out[:, :, ch] = np.roll(img[:, :, ch], off, axis=0)
    # Randzeilen verwerfen, in die durch das Rollen fremde Daten geraten sind
    lo, hi = max(0, max(offsets)), min(0, min(offsets))
    return out[lo:len(out) + hi] if (lo or hi) else out


def orient(img):
    """Bild in Leserichtung bringen.

    Der Sensor liefert die Spalten gespiegelt — genau das besagt der
    Konfigurationswert `HorizontalMirror = 1`.  Die Zeilenrichtung stimmt
    dagegen bereits.  Am Testblatt bestätigt.
    """
    return img[:, ::-1].copy()


def _sheet_edge(rows, win, fraction):
    """Index der ersten Blattzeile in `rows` (vom Anfang her) oder 0."""
    nb = 16
    if len(rows) < 4 * nb:
        return 0
    bg, sd = rows[:nb].mean(), rows[:nb].std()
    if bg > 10 or sd > 0.5:          # kein Vorlauf: Blatt lag schon an
        return 0
    above = np.where(rows > bg + max(0.15, 8 * sd))[0]
    if not len(above):
        return 0
    e = above[0]
    # Helligkeit hinter dem Übergang, nicht in der Rampe selbst (bis ~30 Zeilen)
    behind = rows[e + 2 * win:e + 4 * win]
    if not len(behind):
        behind = rows[e:e + win]
    mid = bg + fraction * (np.median(behind) - bg)
    past = np.where(rows[e:] > mid)[0]
    return int(e + past[0]) if len(past) else 0


def trim_dark(img, fraction=0.5):
    """Dunklen Vor- und Nachlauf abschneiden (Blatt noch nicht / nicht mehr da).

    Bezug ist der Scannerhintergrund aus Vor- und Nachlauf, nicht die hellste
    Zeile: ein randloser, dunkler Abzug hat keine helle Papierkante, sonst
    fielen dunkle Bildzeilen mit weg.  Geschnitten wird bei `fraction` des
    Übergangs zur Helligkeit dahinter — bei weissem Papier die Mitte des
    Übergangs, der je nach Vorlage etwa 8 bis 30 Zeilen dauert.  Ein fester
    Zusatzbeschnitt ist nicht nötig und schneidet sonst Bildinhalt weg: so
    getrimmt ergibt eine A4-Seite exakt 297 mm.  Gleiches Verfahren wie sane/ephoto.c.
    """
    rows = img.mean(axis=(1, 2))
    win = 16 * img.shape[1] // 2550             # 16 Zeilen bei 300 dpi
    a = _sheet_edge(rows, win, fraction)
    b = len(rows) - _sheet_edge(rows[::-1], win, fraction)
    if b <= a:
        return img, 0, len(img)
    return img[a:b], a, b


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("raw")
    ap.add_argument("--out", default=str(pathlib.Path(__file__).resolve().parents[1] / "captures/scan_out.png"))
    ap.add_argument("--no-trim", action="store_true")
    ap.add_argument("--no-rotate", action="store_true")
    ap.add_argument("--preview-width", type=int, default=900)
    args = ap.parse_args()

    img = load_raw(args.raw)
    print(f"roh: {img.shape[1]} x {img.shape[0]}")

    img = fill_segment_gaps(img)
    print(f"Segmentlücken gefüllt: {len(gap_columns())} Spalten "
          f"(Pixel {GAP_FIRST}, dann alle {GAP_PERIOD})")

    img = align_channels(img)
    print(f"Kanalversatz ausgeglichen: R/G/B = {CHANNEL_LINE_OFFSET} Zeilen")

    if not args.no_rotate:
        img = orient(img)
        print("um 180 Grad gedreht (HorizontalMirror + Durchlaufrichtung)")

    if not args.no_trim:
        img, a, b = trim_dark(img)
        print(f"Vor-/Nachlauf abgeschnitten: Zeilen {a}..{b} -> {img.shape[0]} Zeilen")

    Image.fromarray(img).save(args.out)
    print(f"-> {args.out}")

    w = args.preview_width
    h = int(img.shape[0] * w / img.shape[1])
    Image.fromarray(img).resize((w, h), Image.LANCZOS).save(
        args.out.replace(".png", "_preview.png"))
    print(f"-> {args.out.replace('.png', '_preview.png')}  ({w}x{h})")


if __name__ == "__main__":
    main()
