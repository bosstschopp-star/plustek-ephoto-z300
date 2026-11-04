# Linux driver for the Plustek ePhoto Z300

An unofficial, reverse-engineered Linux driver for the **Plustek ePhoto Z300**
sheet-fed photo scanner (USB `07b3:143d`, internally "M21", Genesys Logic
CGL126). Plustek does not provide a Linux driver, and VueScan does not support
the device on Linux.

*Deutsch: siehe [unten](#deutsch).*

> **Status:** works on the author's device. Tested on Ubuntu with one scanner;
> other units may differ. Use at your own risk.
> Not affiliated with or endorsed by Plustek.

## What's included

| Part | Description |
|---|---|
| `sane/` | **SANE backend** in C (libusb). The scanner shows up in GNOME Document Scanner, XSane, GIMP, `scanimage`. 300 and 600 dpi colour, A4. |
| `app/` | **ePhoto Scan** — GTK 4 / libadwaita app: scanning (manual or automatically when a photo is inserted), filters with live preview, zoom, crop, multi-page documents, export to searchable PDF (OCR), TIFF, JPEG, PNG, text. |
| `src/` | Python driver and diagnostic tools (pyusb). |
| `docs/PROTOCOL.md` | The reconstructed USB protocol (German). |
| `tools/` | udev rule, capture parsers; `tools/re/` reverse-engineering scripts. |

### Image processing

The backend corrects two hardware quirks found by comparing against scans
from the vendor's Windows software:

- **Shading layout** — calibration data from the scanner's SPI flash is loaded
  in 42-pixel blocks (504 + 8 bytes); otherwise colour bands appear.
- **Line jitter** — the motor samples lines unevenly with a period of 4,
  causing staircase artefacts. Line positions are estimated per scan and the
  image is resampled (only if the pattern is present).

By default the backend also applies a tone curve matched to the Windows driver
and light sharpening; `--enhance=no` returns linear sensor data.

## Installation

```sh
sudo bash install.sh            # build deps, udev rule, SANE backend
scanimage -L
scanimage -d ephoto:ephoto:z300 --resolution 300 --format=png -o scan.png
```

The app (optional):

```sh
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 tesseract-ocr tesseract-ocr-deu
bash app/install_app.sh         # Python venv + menu entry "ePhoto Scan"
```

Tesseract is only needed for OCR (searchable PDF, text export).

### ePhoto Scan — controls

Mouse wheel zooms around the pointer, drag to pan, double-click toggles fit /
100 %. Keys: `+` `−` `0` `1`, `C` crop (Enter applies, Esc cancels).
The split *Scan* button switches between manual and automatic start (paper
sensor). "Append new scans as page" collects pages into one document.

## Legal

The protocol was reconstructed solely for interoperability with the author's
own hardware (Art. 6 of Directive 2009/24/EC). This repository contains **no
vendor software, firmware or vendor data files**; the reverse-engineering
scripts in `tools/re/` need the vendor framework, which you must obtain
yourself. "Plustek" and "ePhoto" are trademarks of Plustek Inc., used here
only to identify the supported hardware.

## License

GPL-2.0-or-later (see `LICENSE`), compatible with the SANE project.

---

## Deutsch

Inoffizieller, per Reverse Engineering entstandener Linux-Treiber für den
Fotoscanner **Plustek ePhoto Z300**.

- `sane/` — SANE-Backend: Scanner erscheint im Dokumenten-Scanner, XSane, GIMP.
- `app/` — **ePhoto Scan**: Scannen (per Knopf oder automatisch beim Einlegen),
  Filter mit Live-Vorschau (Ton & Farbe, Restaurierung, Schärfe & Rauschen),
  Zoom, Zuschneiden, mehrseitige Dokumente, Export als durchsuchbares PDF,
  TIFF, JPEG, PNG oder Text.
- `docs/PROTOCOL.md` — das rekonstruierte USB-Protokoll.

Installation siehe oben (`sudo bash install.sh`, danach optional
`bash app/install_app.sh`). Getestet mit einem Gerät unter Ubuntu; keine
Verbindung zu Plustek.
