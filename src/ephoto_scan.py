#!/usr/bin/env python3
"""
ephoto-scan — Scannen mit dem Plustek ePhoto Z300 unter Linux.

    ./ephoto_scan.py foto.png
    ./ephoto_scan.py --shading captured --keep-raw roh.bin scan.tiff

Ein Befehl von der Papierkante bis zur Bilddatei. Der Ablauf steckt in
driver.py, die Bildaufbereitung in postprocess.py.

Grenzen: 300 dpi Farbe, Seitenlänge fest (A4). Für andere Auflösungen müsste
die Sequenz parametrisch erzeugt statt als Daten hinterlegt werden.
"""
import argparse
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent))
from driver import (open_device, build_tables, scan, wait_for_paper,  # noqa: E402
                    geometry, settle)
from postprocess import (fill_segment_gaps, align_channels, orient,  # noqa: E402
                         trim_dark)

# Kein fester Zusatzbeschnitt: trim_dark schneidet bereits in der Mitte des
# Kantenübergangs.  `DiscardLine = 54` aus der INI meint zu verwerfende Zeilen
# beim Sensorauslesen, nicht einen Beschnitt des fertigen Bildes.
DISCARD_LINES = 0


def to_image(raw, mirror=True, discard=DISCARD_LINES, dpi=300):
    g = geometry(dpi)
    lines = len(raw) // g["stride"]
    img = np.frombuffer(raw[:lines * g["stride"]], dtype=np.uint8)
    img = img.reshape(lines, g["pixels"], 3)
    img = fill_segment_gaps(img)
    img = align_channels(img)
    if mirror:
        img = orient(img)
    img, _, _ = trim_dark(img)
    if discard and img.shape[0] > 2 * discard:
        img = img[discard:-discard]
    return img


def main():
    ap = argparse.ArgumentParser(description="Scannen mit dem Plustek ePhoto Z300")
    ap.add_argument("out", nargs="?", default="scan.png", help="Ausgabedatei")
    ap.add_argument("--dpi", type=int, choices=(300, 600), default=300,
                    help="Auflösung (Vorgabe 300)")
    ap.add_argument("--wait", type=float, default=45.0,
                    help="Sekunden auf ein Blatt warten")
    ap.add_argument("--shading", choices=("flash", "captured", "none", "flat"),
                    default="flash",
                    help="Quelle der Kalibrierdaten (Vorgabe: aus dem Gerät)")
    ap.add_argument("--no-mirror", action="store_true",
                    help="Horizontalspiegelung weglassen")
    ap.add_argument("--keep-raw", metavar="DATEI")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        dev = open_device()
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Scanner nicht erreichbar: {e}")

    say = (lambda *a: None) if args.quiet else print

    if not wait_for_paper(dev, args.wait,
                          lambda: say(f"Bitte Blatt einlegen "
                                      f"(warte bis {args.wait:.0f} s) ...")):
        sys.exit("Kein Blatt eingelegt.")

    say(f"Tabellen vorbereiten (Shading: {args.shading}) ...")
    tables = build_tables(dev, shading=args.shading, dpi=args.dpi)

    say("Scanne ...")
    last = [0.0]

    def prog(n, t):
        if t - last[0] > 0.5:
            last[0] = t
            say(f"\r  {n/1048576:5.1f} MiB  {n//geometry(args.dpi)['stride']:5d} Zeilen "
                f"({t:4.1f} s)", end="", flush=True)

    raw = scan(dev, tables, progress=None if args.quiet else prog, dpi=args.dpi)
    # scan() ruft settle() bereits im finally auf; hier nur noch messen,
    # wie lange der Motor nach dem Abschluss noch lief

    say("")
    if not raw:
        sys.exit("Keine Bilddaten empfangen.")
    if args.keep_raw:
        Path(args.keep_raw).write_bytes(raw)

    img = to_image(raw, mirror=not args.no_mirror, dpi=args.dpi)
    if img.size == 0 or img.shape[0] < 4:
        sys.exit("Bild leer — vermutlich zu dunkel (Shading?).")
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(args.out)
    say(f"{img.shape[1]} x {img.shape[0]} -> {args.out}")


if __name__ == "__main__":
    main()
