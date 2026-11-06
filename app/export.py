"""
Export für ePhoto Scan: eine oder mehrere Seiten in eine Datei bzw. Dateien.

Formate
    pdf        PDF, durchsuchbar — Bilder als JPEG, darunter eine unsichtbare
               Textebene aus der Texterkennung (Tesseract)
    pdf-plain  PDF nur mit Bildern
    tiff       TIFF, alle Seiten in einer Datei
    jpg / png  je Seite eine Datei (name_01.jpg, name_02.jpg, …)
    txt        nur der erkannte Text, Seiten durch Seitenvorschub getrennt

Seiten sind Objekte mit `src`, `dj` (entzerrt oder None), `dpi` und `params`
(filters.Params) — gerechnet wird hier, nicht in der Oberfläche.
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import filters as F
from i18n import N_, _, system_languages

PREVIEW_MAX = 1400

# (Schlüssel, Bezeichnung, Endung, braucht Texterkennung)
FORMATS = (
    ("pdf", N_("PDF, searchable (with text recognition)"), ".pdf", True),
    ("pdf-plain", N_("PDF, image only"), ".pdf", False),
    ("tiff", N_("TIFF, all pages in one file"), ".tif", False),
    ("jpg", N_("JPEG, one file per page"), ".jpg", False),
    ("png", N_("PNG, one file per page"), ".png", False),
    ("txt", N_("Text (recognized text only)"), ".txt", True),
)
FORMAT_BY_KEY = {f[0]: f for f in FORMATS}
# Anzeigenamen (zum Übersetzen markiert, übersetzt wird in der Oberfläche)
LANG_NAMES = {"deu": N_("German"), "eng": N_("English"), "fra": N_("French"), "ita": N_("Italian"),
              "spa": N_("Spanish"), "nld": N_("Dutch"), "deu_latf": N_("German (Fraktur)"), "frk": N_("Fraktur")}
# Systemsprache (ISO 639-1) -> Tesseract-Sprache
_TESS_CODES = {"de": "deu", "en": "eng", "fr": "fra", "it": "ita", "es": "spa", "nl": "nld"}


def default_ocr_lang(langs):
    """Voreinstellung der Texterkennung passend zur Systemsprache, aus den installierten `langs`."""
    own = next((_TESS_CODES[lang] for lang in system_languages() if lang in _TESS_CODES), None)
    if own == "deu" and "deu+eng" in langs:
        return "deu+eng"
    if own and own in langs:
        return f"{own}+eng" if own != "eng" and "eng" in langs else own
    return "eng" if "eng" in langs else langs[0]


def tesseract_langs():
    """Installierte Sprachen der Texterkennung, oder None ohne Tesseract."""
    if not shutil.which("tesseract"):
        return None
    r = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    langs = [line.strip() for line in r.stdout.splitlines()[1:] if line.strip()]
    return [lang for lang in langs if lang != "osd"]


def render_page(page):
    """Seite in voller Auflösung mit ihren Einstellungen rechnen.  Liefert ein PIL-Bild."""
    p = page.params
    src = page.dj if (p.dejitter and page.dj is not None) else page.src
    s = min(1.0, PREVIEW_MAX / max(src.shape[:2]))
    proxy = cv2.resize(src, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 1 else src
    stats = {}
    F.process(proxy, p, s, stats)              # Kennwerte wie in der Vorschau
    img = Image.fromarray(F.process(src, p, 1.0, stats))
    return img.convert("L") if p.color_mode == "sw" else img


def _numbered(path, i, n):
    return path if n == 1 else path.with_name(f"{path.stem}_{i + 1:02d}{path.suffix}")


def _ocr(image_files, outbase, lang, renderers):
    """Tesseract über alle Seiten; `renderers` z. B. ["pdf", "txt"]."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lst:
        lst.write("\n".join(str(f) for f in image_files) + "\n")
    try:
        r = subprocess.run(["tesseract", lst.name, str(outbase), "-l", lang, *renderers],
                           capture_output=True, text=True)
    finally:
        Path(lst.name).unlink(missing_ok=True)
    if r.returncode:
        raise RuntimeError(_("Text recognition failed: ") + (r.stderr.strip().splitlines() or ["?"])[-1])


def ocr_text(img, lang, dpi):
    """Text eines einzelnen Bildes (PIL) erkennen."""
    with tempfile.TemporaryDirectory(prefix="ephoto-ocr-") as tmp:
        f = Path(tmp) / "seite.png"
        img.save(f, dpi=(dpi, dpi))
        _ocr([f], Path(tmp) / "out", lang, ["txt"])
        return (Path(tmp) / "out.txt").read_text(encoding="utf-8")


def export(pages, path, fmt, lang="deu", quality=90, progress=None):
    """Seiten exportieren.  Liefert die Liste der geschriebenen Dateien."""
    path = Path(path)
    key, _label, ext, _needs_ocr = FORMAT_BY_KEY[fmt]
    if path.suffix.lower() != ext and not (ext == ".jpg" and path.suffix.lower() == ".jpeg") \
            and not (ext == ".tif" and path.suffix.lower() == ".tiff"):
        path = path.with_suffix(ext)
    n = len(pages)
    say = progress or (lambda *_: None)

    if key in ("jpg", "png"):
        out = []
        for i, pg in enumerate(pages):
            say(_("Page {i} of {n} …").format(i=i + 1, n=n))
            img = render_page(pg)
            f = _numbered(path, i, n)
            kw = {"quality": quality, "subsampling": 0} if key == "jpg" else {}
            img.save(f, dpi=(pg.dpi, pg.dpi), **kw)
            out.append(f)
        return out

    with tempfile.TemporaryDirectory(prefix="ephoto-export-") as tmp:
        tmp = Path(tmp)
        files = []
        for i, pg in enumerate(pages):        # Seiten einzeln rechnen und ablegen: wenig Speicher
            say(_("Page {i} of {n} …").format(i=i + 1, n=n))
            img = render_page(pg)
            if key == "tiff":
                f = tmp / f"s{i:03d}.tif"
                img.save(f, dpi=(pg.dpi, pg.dpi), compression="tiff_deflate")
            else:
                f = tmp / f"s{i:03d}.jpg"      # Tesseract übernimmt JPEG unverändert ins PDF
                img.save(f, dpi=(pg.dpi, pg.dpi), quality=quality, subsampling=0)
            files.append(f)

        if key == "tiff":
            first, *rest = (Image.open(f) for f in files)
            first.save(path, save_all=True, append_images=rest, compression="tiff_deflate")
        elif key == "pdf-plain":
            first, *rest = (Image.open(f) for f in files)
            first.save(path, "PDF", save_all=True, append_images=rest, resolution=float(pages[0].dpi),
                       quality=quality)
        elif key == "pdf":
            say(_("Recognizing text …"))
            _ocr(files, tmp / "out", lang, ["pdf"])
            shutil.move(tmp / "out.pdf", path)
        elif key == "txt":
            say(_("Recognizing text …"))
            _ocr(files, tmp / "out", lang, ["txt"])
            shutil.move(tmp / "out.txt", path)
        return [path]
