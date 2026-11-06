"""
Bildfilter für ePhoto Scan — reines numpy/OpenCV, ohne GUI.

Die Pipeline arbeitet auf RGB (uint8 rein, uint8 raus) in fester Reihenfolge:

    Geometrie        Drehen, Begradigen, Zuschneiden
    Ton & Farbe      Scanner-Kennlinie, Verblassung, Tonwert, Helligkeit/
                     Kontrast/Gamma, Weissabgleich, Sättigung, Farbmodus
    Restaurierung    lokaler Kontrast, Staub & Kratzer
    Schärfe/Rauschen Entrauschen, Filmkorn glätten, Nachschärfen

Räumliche Parameter sind in Pixeln der vollen Auflösung angegeben; `scale`
rechnet sie auf eine verkleinerte Vorschau um.  Bildabhängige Kennwerte
(Tonwertgrenzen, Zuschnitt) stehen in `stats`: einmal an der Vorschau
ermittelt, gelten sie unverändert auch für 1:1-Ausschnitt und Export — sonst
sähe jeder Ausschnitt anders aus.
"""
from dataclasses import asdict, dataclass, fields, replace

import cv2
import numpy as np

from i18n import N_

# Tonwertkurve des Z300, am Windows-Scan gemessen (siehe sane/enhance.h)
SCANNER_CURVE_X = list(range(0, 256, 16)) + [255]
SCANNER_CURVE_Y = [39.9, 64.2, 101.0, 124.9, 143.7, 158.4, 171.4, 182.6, 193.1,
                   202.7, 211.5, 219.4, 226.7, 233.4, 239.2, 244.9, 249.2]
SCANNER_LUT = np.interp(np.arange(256), SCANNER_CURVE_X, SCANNER_CURVE_Y).astype(np.float32)

SEPIA = np.float32([1.07, 0.95, 0.76])
COLOR_MODES = ("farbe", "sw", "sepia")


@dataclass
class Params:
    # Zeilenversatz (Treppen) — wird einmal je Bild vorab gerechnet, siehe dejitter()
    dejitter: bool = True
    # Geometrie
    rotate: int = 0                 # 0, 90, 180, 270 (im Uhrzeigersinn)
    straighten: float = 0.0         # Grad
    auto_crop: bool = True          # auf das Foto zuschneiden (dunkler Hintergrund)
    trim_border: bool = True        # dabei auch einen Papierrand des Abzugs entfernen
    crop_margin: int = 8            # Pixel nach innen (nur beim automatischen Zuschnitt)
    crop: tuple = None              # von Hand: (x0, y0, x1, y1) als Bruchteile, hat Vorrang
    # Ton & Farbe
    scanner_curve: bool = True      # Kennlinie für lineare Rohdaten
    fade_restore: float = 0.0       # 0..1  Farbstich/Verblassung je Kanal ausgleichen
    auto_levels: float = 0.0        # 0..1  Schwarz-/Weisspunkt automatisch
    brightness: float = 0.0         # -1..1
    contrast: float = 0.0           # -1..1
    gamma: float = 1.0              # 0.5..2
    temperature: float = 0.0        # -1 kalt .. 1 warm
    tint: float = 0.0               # -1 grün .. 1 magenta
    saturation: float = 1.0         # 0..2
    color_mode: str = "farbe"       # farbe, sw, sepia
    # Restaurierung
    local_contrast: float = 0.0     # 0..1  CLAHE
    dust: float = 0.0               # 0..1  Staub & Kratzer
    # Schärfe & Rauschen
    denoise: float = 0.0            # 0..1  Non-Local Means
    grain: float = 0.0              # 0..1  kantenerhaltend glätten
    sharpen: float = 0.3            # 0..2  Unscharfmaske, Stärke
    sharpen_radius: float = 1.0     # Pixel

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        p = cls(**{k: v for k, v in d.items() if k in names})
        return replace(p, crop=tuple(p.crop)) if p.crop else p

    def uncropped(self):
        """Gleiche Einstellungen ohne Zuschnitt — Grundlage für das Zuschneide-Werkzeug."""
        return replace(self, crop=None, auto_crop=False)


PRESETS = {
    N_("Photo (like Windows)"): Params(),
    N_("Restore old photo"): Params(fade_restore=0.5, auto_levels=0.3, local_contrast=0.2,
                                      dust=0.4, denoise=0.1, sharpen=0.35),
    N_("Black-and-white photo"): Params(color_mode="sw", auto_levels=0.4, local_contrast=0.25,
                                dust=0.4, denoise=0.1, sharpen=0.35),
    # bei Dokumenten gehört der weisse Rand zur Seite
    N_("Document / text"): Params(auto_levels=1.0, contrast=0.3, sharpen=0.8, trim_border=False),
    N_("No processing"): Params(scanner_curve=False, sharpen=0.0, auto_crop=False),
}


# --- Zeilenversatz ------------------------------------------------------------
# Der Z300 tastet die Zeilen mit Periode 4 ungleichmässig ab (gemessen etwa
# 0 / 2,02 / 2,40 / 2,49 px statt 0 / 1 / 2 / 3) — Treppen an schrägen Kanten.
# Das neue SANE-Backend korrigiert das schon beim Scannen; für ältere Dateien
# geschieht es hier.  Gleiches Verfahren wie sane/enhance.h.
JITTER_PERIOD = 4


def _resample_rows(img, offs, period=JITTER_PERIOD):
    """Zeile r liegt bei r + offs[r % period]; auf gleichmässiges Raster bringen."""
    h, w = img.shape[:2]
    pos = np.arange(h) + np.asarray(offs)[np.arange(h) % period]
    src = np.interp(np.arange(h, dtype=float), pos, np.arange(h, dtype=float))
    map_y = np.repeat(src[:, None], w, 1).astype(np.float32)
    map_x = np.repeat(np.arange(w, dtype=np.float32)[None, :], h, 0)
    return cv2.remap(img, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


def _phase_variation(Y, offs, period=JITTER_PERIOD):
    d = np.abs(np.diff(_resample_rows(Y, offs, period), axis=0)).mean(1)
    ph = np.array([d[p::period].mean() for p in range(period)])
    return ph.std() / ph.mean()


def dejitter(img, linear=True, period=JITTER_PERIOD):
    """Zeilenlagen schätzen und neu abtasten.  Liefert (Bild, Lagen, vorher, nachher);
    ist kein Muster vorhanden, kommt das Bild unverändert zurück."""
    f = SCANNER_LUT[img] if linear else img.astype(np.float32)
    Y = f @ np.float32([0.114, 0.587, 0.299][::-1])         # RGB
    h, w = Y.shape
    y0 = h // 10 // period * period             # phasengleich zum ganzen Bild beginnen
    sample = Y[y0:h * 9 // 10, w // 8:w * 7 // 8:4]
    offs = np.zeros(period)
    base = before = _phase_variation(sample, offs, period)
    for step in (0.2, 0.05, 0.01):
        better = True
        while better:
            better = False
            for p in range(1, period):
                for sgn in (-step, step):
                    t = offs.copy(); t[p] += sgn
                    pos = np.arange(period + 1) + np.append(t, t[0])
                    if np.any(np.diff(pos) < 0):    # Zeilen dürfen sich nicht überholen
                        continue
                    v = _phase_variation(sample, t, period)
                    if v < base - 1e-6:
                        offs, base, better = t, v, True
    # gemessen: Windows-Scan 0,006, Linux roh 0,43, Linux entzerrt 0,016
    if before < 0.05 or base > before * 0.5:
        return img, offs, before, base
    return _resample_rows(img, offs, period), offs, before, base


# --- Geometrie ---------------------------------------------------------------
def _background_box(Y):
    """Foto vor dem dunklen Scannerhintergrund: (x0, y0, x1, y1) in Pixeln von Y oder None."""
    ring = np.concatenate([Y[:4].ravel(), Y[-4:].ravel(), Y[:, :4].ravel(), Y[:, -4:].ravel()])
    bg, top = np.median(ring), np.percentile(Y, 95)
    # Hintergrund liegt vor, wenn der Rand ringsum überwiegend gleichmässig dunkel ist —
    # nicht über den Bildmedian prüfen: bei kleinem Foto auf A4 ist der selbst schwarz
    if np.mean(np.abs(ring - bg) < 12) < 0.5 or top - bg < 40 or bg > 0.4 * top:
        return None
    # alles, was sich vom gleichmässigen Hintergrund abhebt — auch dunkle Bildpartien
    spread = 1.4826 * np.median(np.abs(ring - bg))
    mask = (np.abs(Y - bg) > max(8.0, 4 * spread)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    n, _lab, st, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if n < 2:
        return None
    areas = st[1:, cv2.CC_STAT_AREA]
    if areas.max() < 0.05 * mask.size:
        return None
    # Teile des Fotos, die dunkle Partien vom Rest trennen, wieder zusammenfassen
    keep = [i + 1 for i, a in enumerate(areas) if a >= 0.02 * areas.max()]
    x0 = min(st[i, cv2.CC_STAT_LEFT] for i in keep)
    y0 = min(st[i, cv2.CC_STAT_TOP] for i in keep)
    x1 = max(st[i, cv2.CC_STAT_LEFT] + st[i, cv2.CC_STAT_WIDTH] for i in keep)
    y1 = max(st[i, cv2.CC_STAT_TOP] + st[i, cv2.CC_STAT_HEIGHT] for i in keep)
    return x0, y0, x1, y1


def _paper_border(Y, max_frac=0.2):
    """Bildinhalt innerhalb eines gleichmässigen Papierrands (weiss, creme, Büttenrand)
    und eventueller Reste von Scannerhintergrund.  Liefert (links, oben, rechts, unten)
    als Pixelzahl, die je Seite wegfällt.

    Papierrand ist hell, strukturlos und hängt mit dem äusseren Bildrand zusammen —
    helle Flächen im Motiv tun Letzteres nicht.  Geschnitten wird erst, wo kaum noch
    Rand in der Linie liegt: Abzüge sind oft leicht schief beschnitten, sonst bliebe
    an einem Ende der Kante ein Keil Papier stehen.  Ein dunkler Streifen wird nur
    entfernt, wenn danach Papierrand folgt oder er sehr schmal ist — ein dunkler
    Himmel am Bildrand bleibt also stehen."""
    h, w = Y.shape
    g = cv2.GaussianBlur(Y, (0, 0), 1.0)
    grad = np.hypot(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    hi = np.percentile(Y, 99)
    ring = np.concatenate([Y[:8].ravel(), Y[-8:].ravel(), Y[:, :8].ravel(), Y[:, -8:].ravel()])
    bright = ring[ring > 0.6 * hi]
    if len(bright) < 50:
        return 0, 0, 0, 0
    paper = np.median(bright)
    # Papier von Abzügen ist hell (gemessen 208 roh, 233 aufbereitet) und gehört zum
    # Hellsten im Bild — sonst ist es kein Rand, sondern Motiv
    if paper < 150 or paper < 0.85 * np.percentile(Y, 99.5):
        return 0, 0, 0, 0
    tol = max(0.06 * paper, 3 * 1.4826 * np.median(np.abs(bright - paper)))
    plain = (np.abs(Y - paper) < tol) & (grad < 0.25 * paper)
    dark = (Y < 0.25 * paper) & (grad < 0.25 * paper)
    # Übergangslinien zwischen Hintergrund und Rand (1–2 px, weder das eine noch das
    # andere) überbrücken, sonst gilt der Rand als vom Bildrand abgetrennt
    bridged = cv2.dilate((plain | dark).astype(np.uint8), np.ones((3, 3), np.uint8))
    _n, lab = cv2.connectedComponents(bridged, connectivity=4)
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    outer = np.isin(lab, edge[edge > 0])
    plain &= outer
    dark &= outer

    def side(line, limit):
        i = 0
        while i < limit and line(dark, i).mean() >= 0.6:      # Scannerhintergrund
            i += 1
        dark_end = i
        stop = min(limit, i + int(0.08 * limit / max_frac))   # Papierrand höchstens ~8 %

        def is_border(j):
            return line(plain, j).mean() >= 0.12 or line(dark, j).mean() >= 0.6
        while i < stop:
            # Inhalt erst, wenn er ein paar Linien anhält (Übergänge sind 2–3 Linien breit)
            if not is_border(i) and all(not is_border(j) for j in range(i + 1, min(i + 6, limit))):
                break
            i += 1
        return i if (i > dark_end or dark_end <= max(2, 0.03 * limit / max_frac)) else 0

    return (side(lambda m, i: m[:, i], int(w * max_frac)), side(lambda m, i: m[i], int(h * max_frac)),
            side(lambda m, i: m[:, w - 1 - i], int(w * max_frac)), side(lambda m, i: m[h - 1 - i], int(h * max_frac)))


def find_photo(img, trim_border=False):
    """Rechteck des Fotos als Bruchteile (x0, y0, x1, y1) der Bildgrösse — damit gilt
    es für jede Auflösung — oder None, wenn nichts abzuschneiden ist."""
    H, W = img.shape[:2]
    f = 800 / max(H, W)
    small = cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    Y = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32)
    h, w = Y.shape
    box = _background_box(Y)
    # zurück in Bruchteile; der Papierrand wird dann auf dem ausgeschnittenen Foto in
    # eigener Auflösung gesucht — auf der verkleinerten A4-Seite wäre er nur wenige Pixel breit
    fx0, fy0, fx1, fy1 = (box[0] / w, box[1] / h, box[2] / w, box[3] / h) if box else (0.0, 0.0, 1.0, 1.0)
    if trim_border:
        X0, Y0, X1, Y1 = int(fx0 * W), int(fy0 * H), int(fx1 * W), int(fy1 * H)
        region = img[Y0:Y1, X0:X1]
        if min(region.shape[:2]) > 64:
            g = 800 / max(region.shape[:2])
            rs = cv2.resize(region, None, fx=g, fy=g, interpolation=cv2.INTER_AREA) if g < 1 else region
            rY = cv2.cvtColor(rs, cv2.COLOR_RGB2GRAY).astype(np.float32)
            rh, rw = rY.shape
            l, t, r, b = _paper_border(rY)
            fx0, fx1 = fx0 + (fx1 - fx0) * l / rw, fx0 + (fx1 - fx0) * (rw - r) / rw
            fy0, fy1 = fy0 + (fy1 - fy0) * t / rh, fy0 + (fy1 - fy0) * (rh - b) / rh
    if (fx0, fy0, fx1, fy1) == (0.0, 0.0, 1.0, 1.0):
        return None
    return (fx0, fy0, fx1, fy1)


def geometry(img, p, stats, scale=1.0):
    if p.rotate:
        img = np.ascontiguousarray(np.rot90(img, k=-(p.rotate // 90) % 4))
    if abs(p.straighten) > 0.01:
        h, w = img.shape[:2]
        M = cv2.getRotationMatrix2D((w / 2, h / 2), -p.straighten, 1.0)
        img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    if p.crop:
        h, w = img.shape[:2]
        x0, y0, x1, y1 = p.crop
        x0, x1 = sorted((int(round(x0 * w)), int(round(x1 * w))))
        y0, y1 = sorted((int(round(y0 * h)), int(round(y1 * h))))
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(w, x1), min(h, y1)
        if x1 - x0 > 8 and y1 - y0 > 8:
            img = img[y0:y1, x0:x1]
    elif p.auto_crop:
        key = ("crop", p.rotate, round(p.straighten, 2), p.trim_border)
        if key not in stats:
            stats[key] = find_photo(img, p.trim_border)
        box = stats[key]
        if box:
            h, w = img.shape[:2]
            m = int(round(p.crop_margin * scale))
            x0, y0 = max(0, int(box[0] * w) + m), max(0, int(box[1] * h) + m)
            x1, y1 = min(w, int(box[2] * w) - m), min(h, int(box[3] * h) - m)
            if x1 - x0 > 16 and y1 - y0 > 16:
                img = img[y0:y1, x0:x1]
    return img


# --- Ton & Farbe ---------------------------------------------------------------
def _lum(f):
    return f @ np.float32([0.299, 0.587, 0.114])


def _stretch(f, lo, hi):
    return (f - lo) * (255.0 / np.maximum(hi - lo, 1.0))


def tone_color(img, p, stats):
    f = SCANNER_LUT[img] if p.scanner_curve else img.astype(np.float32)

    if p.fade_restore > 0:
        if "fade" not in stats:
            s = f[::4, ::4].reshape(-1, 3)
            stats["fade"] = (np.percentile(s, 0.5, axis=0), np.percentile(s, 99.9, axis=0))
        lo, hi = stats["fade"]
        f = f + p.fade_restore * (_stretch(f, lo, hi) - f)

    if p.auto_levels > 0:
        if "levels" not in stats:
            Y = _lum(f[::4, ::4])
            # oben knapp unter dem Maximum: helle Flächen (weisse Kleider, Papier)
            # sollen nicht ausbrennen
            stats["levels"] = (np.percentile(Y, 0.3), np.percentile(Y, 99.95))
        lo, hi = stats["levels"]
        f = f + p.auto_levels * (_stretch(f, lo, hi) - f)

    if p.brightness:
        f = f + 60.0 * p.brightness
    if p.contrast:
        f = (f - 128.0) * (1.0 + p.contrast) + 128.0
    if abs(p.gamma - 1.0) > 1e-3:
        f = 255.0 * np.power(np.clip(f, 0, 255) / 255.0, 1.0 / p.gamma)
    if p.temperature or p.tint:
        t, g = p.temperature, p.tint
        f = f * np.float32([1 + 0.12 * t, 1 - 0.08 * g, 1 - 0.12 * t])

    if p.color_mode in ("sw", "sepia"):
        Y = _lum(f)[..., None]
        f = Y * (SEPIA if p.color_mode == "sepia" else np.float32([1, 1, 1]))
    elif abs(p.saturation - 1.0) > 1e-3:
        Y = _lum(f)[..., None]
        f = Y + p.saturation * (f - Y)
    return np.clip(f + 0.5, 0, 255).astype(np.uint8)


# --- Restaurierung ---------------------------------------------------------------
def local_contrast(img, p, scale):
    if p.local_contrast <= 0:
        return img
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
    # Kachelgrösse in Vollauflösung fest (~300 px), damit Vorschau, Ausschnitt
    # und Export gleich aussehen
    tile = max(8, int(300 * scale))
    grid = (max(1, img.shape[1] // tile), max(1, img.shape[0] // tile))
    clahe = cv2.createCLAHE(clipLimit=1.0 + 3.0 * p.local_contrast, tileGridSize=grid)
    L = clahe.apply(lab[:, :, 0])
    lab[:, :, 0] = cv2.addWeighted(L, p.local_contrast, lab[:, :, 0], 1 - p.local_contrast, 0)
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def dust(img, p, scale):
    """Kleine helle/dunkle Punkte und dünne Kratzer durch den lokalen Median ersetzen."""
    if p.dust <= 0:
        return img
    k = max(3, int(round(7 * scale)) | 1)
    med = cv2.medianBlur(img, k)
    Y, Ym = (cv2.cvtColor(a, cv2.COLOR_RGB2GRAY).astype(np.int16) for a in (img, med))
    thr = 45 - 30 * p.dust                       # Stärke 1 -> schon 15 Stufen Abweichung
    mask = (np.abs(Y - Ym) > thr).astype(np.uint8)
    # nur kleine Flecken: was eine Öffnung mit k überlebt, ist Struktur, kein Staub
    big = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    mask = cv2.dilate(mask & (1 - big), np.ones((3, 3), np.uint8))
    out = img.copy()
    out[mask > 0] = med[mask > 0]
    return out


# --- Schärfe & Rauschen -----------------------------------------------------------
def noise_sharp(img, p, scale):
    if p.denoise > 0:
        # Feines Filmkorn ist bei alten Abzügen zugleich Bilddetail.  Volle
        # Filterstärke macht Gesichter zum Aquarell; deshalb mild filtern und
        # mit dem Original überblenden.
        h = 1.5 + 6 * p.denoise
        nlm = cv2.fastNlMeansDenoisingColored(img, None, h, h, 7, 21)
        img = cv2.addWeighted(nlm, 0.4 + 0.6 * p.denoise, img, 0.6 - 0.6 * p.denoise, 0)
    if p.grain > 0:
        d = max(3, int(round(7 * scale)))
        sm = cv2.bilateralFilter(img, d, 8 + 30 * p.grain, max(1.0, 3 * scale))
        img = cv2.addWeighted(sm, p.grain, img, 1 - p.grain, 0)
    if p.sharpen > 0:
        sigma = max(0.3, p.sharpen_radius * scale)
        f = img.astype(np.float32)
        f = f + p.sharpen * (f - cv2.GaussianBlur(f, (0, 0), sigma))
        img = np.clip(f + 0.5, 0, 255).astype(np.uint8)
    return img


def process(img, p, scale=1.0, stats=None, geometry_done=False):
    """Gesamte Pipeline.  `stats` wird ergänzt und kann wiederverwendet werden."""
    if stats is None:
        stats = {}
    if not geometry_done:
        img = geometry(img, p, stats, scale)
    img = tone_color(img, p, stats)
    img = local_contrast(img, p, scale)
    img = dust(img, p, scale)
    img = noise_sharp(img, p, scale)
    return img
