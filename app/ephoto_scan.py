#!/usr/bin/env python3
"""
ePhoto Scan — Scannen mit dem Plustek ePhoto Z300 und Nachbearbeitung mit
Live-Vorschau (GTK 4 / libadwaita).

Gescannt wird über das SANE-Backend mit `--enhance=no`: die App bekommt die
linearen Sensordaten und bereitet sie selbst auf (filters.py).  Bereits
gespeicherte Bilder lassen sich ebenso öffnen und nachbearbeiten.

    ephoto_scan.py [BILD]
    ephoto_scan.py --selftest BILD SCREENSHOT.png [VOREINSTELLUNG]
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import cv2
import gi
import numpy as np
from PIL import Image

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import filters as F  # noqa: E402
import export as X  # noqa: E402
import paper as PW  # noqa: E402
import viewer as V  # noqa: E402

APP_ID = "io.github.bosstschopp_star.EphotoScan"
DEVICE = "ephoto:ephoto:z300"
PREVIEW_MAX = 1400               # längste Seite der Vorschau in Pixeln
# Regler, deren Wirkung nur hineingezoomt zu sehen ist
DETAIL_FIELDS = {"dust", "denoise", "grain", "sharpen", "sharpen_radius"}
# Seitenverhältnisse fürs Zuschneiden: (Name, lange/kurze Seite oder None)
ASPECTS = (("Frei", None), ("Wie Original", "orig"), ("Quadrat 1:1", 1.0), ("10 × 15 (2:3)", 1.5),
           ("13 × 18 (5:7)", 1.4), ("4:5", 1.25), ("A4", 2 ** 0.5))
CONFIG = Path(GLib.get_user_config_dir()) / "ephoto-scan" / "settings.json"


def to_texture(arr):
    h, w = arr.shape[:2]
    arr = np.ascontiguousarray(arr)
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.R8G8B8, GLib.Bytes.new(arr.tobytes()), w * 3)


class Page:
    """Eine Seite des Dokuments: Rohbild, entzerrte Fassung, eigene Einstellungen."""

    def __init__(self, src, dj, before, dpi, name, processed, params):
        self.src, self.dj, self.before = src, dj, before
        self.dpi, self.name, self.processed, self.params = dpi, name, processed, params
        self.proxies = {}
        self.thumb = None


class Worker:
    """Rechnet im Hintergrund; nur der jeweils neueste Auftrag zählt."""

    def __init__(self):
        self._cond = threading.Condition()
        self._job = None
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, fn, done):
        with self._cond:
            self._job = (fn, done)
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while self._job is None:
                    self._cond.wait()
                fn, done = self._job
                self._job = None
            try:
                res, err = fn(), None
            except Exception as e:  # noqa: BLE001
                res, err = None, e
            GLib.idle_add(done, res, err)


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="ePhoto Scan", default_width=1400, default_height=900)
        self.full = None             # RGB, volle Auflösung
        self.proxy = None
        self.proxy_scale = 1.0
        self.dpi = 600
        self.source_name = "scan"
        self.params = F.Params()
        self._geom_cache = None      # (Schlüssel, geometrisch bearbeitetes Vollbild)
        self._last = None            # (Parameter, Kennwerte) der zuletzt gerechneten Vorschau
        self.cropping = False
        self._updating = False
        self._pending = None
        self._setters = {}
        self._reset_btns = {}
        self._detail_hint_shown = False
        self.worker = Worker()           # Vorschau
        self.detail_worker = Worker()    # Ausschnitt in voller Qualität
        self.on_preview_done = None  # Haken für den Selbsttest
        self._dirty = False          # Dokument seit dem Scan nicht gespeichert
        self._previous = None
        self.pages = []              # Seiten des Dokuments
        self.cur = -1                # aktuelle Seite
        self._load_settings()
        self.watcher = PW.PaperWatcher(on_paper=lambda: GLib.idle_add(self._auto_scan),
                                       on_state=lambda t: GLib.idle_add(self._watch_state, t))
        self._build()
        self._set_scan_mode(self.settings.get("scan_mode", "manual"))

    # ---- Aufbau ------------------------------------------------------------
    def _build(self):
        header = Adw.HeaderBar()

        scan_menu = Gio.Menu()
        modes = Gio.Menu()
        modes.append("Per Knopf scannen", "win.scan-mode::manual")
        modes.append("Automatisch beim Einlegen", "win.scan-mode::auto")
        scan_menu.append_section("Scanstart", modes)
        multi = Gio.Menu()
        multi.append("Neue Scans als weitere Seite anhängen", "win.multipage")
        multi.append("Neues Dokument beginnen", "win.new-doc")
        scan_menu.append_section("Mehrseitige Dokumente", multi)
        self.scan_btn = Adw.SplitButton(label="Scannen", menu_model=scan_menu,
                                        dropdown_tooltip="Per Knopf oder automatisch scannen")
        self.scan_btn.add_css_class("suggested-action")
        self.scan_btn.connect("clicked", lambda *_: self.scan())
        mode = Gio.SimpleAction.new_stateful("scan-mode", GLib.VariantType.new("s"),
                                             GLib.Variant("s", "manual"))
        mode.connect("change-state", lambda a, v: self._set_scan_mode(v.get_string()))
        self.add_action(mode)
        self.multipage = bool(self.settings.get("multipage", False))
        mp = Gio.SimpleAction.new_stateful("multipage", None, GLib.Variant("b", self.multipage))
        mp.connect("change-state", self._on_multipage)
        self.add_action(mp)
        self.title = Adw.WindowTitle(title="ePhoto Scan")
        header.set_title_widget(self.title)
        self.res_dd = Gtk.DropDown.new_from_strings(["300 dpi", "600 dpi"])
        self.res_dd.set_selected(1 if self.settings.get("dpi", 600) == 600 else 0)
        self.res_dd.set_tooltip_text("Auflösung")
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Bild öffnen (Strg+O)")
        open_btn.connect("clicked", lambda *_: self.open_dialog())
        header.pack_start(self.scan_btn)
        header.pack_start(self.res_dd)
        header.pack_start(open_btn)

        menu = Gio.Menu()
        menu.append("Text erkennen (aktuelle Seite) …", "win.ocr")
        menu.append("Rohscan speichern …", "win.save-raw")
        menu.append("Über ePhoto Scan", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        self.save_btn = Gtk.Button(label="Speichern", tooltip_text="Bearbeitetes Bild speichern (Strg+S)")
        self.save_btn.connect("clicked", lambda *_: self.save_dialog())
        header.pack_end(self.save_btn)
        zoombox = Gtk.Box(css_classes=["linked"])
        zout = Gtk.Button(icon_name="zoom-out-symbolic", tooltip_text="Verkleinern (−)")
        zout.connect("clicked", lambda *_: self.view.zoom_by(1 / 1.5))
        self.zoom_btn = Gtk.MenuButton(label="Ganz", tooltip_text="Zoomstufe", width_request=72)
        zmenu = Gio.Menu()
        zmenu.append("Ganzes Bild (0)", "win.zoom-fit")
        for pct in (50, 100, 200, 400):
            zmenu.append(f"{pct} %" + (" (1)" if pct == 100 else ""), f"win.zoom-to({pct})")
        self.zoom_btn.set_menu_model(zmenu)
        zin = Gtk.Button(icon_name="zoom-in-symbolic", tooltip_text="Vergrössern (+)")
        zin.connect("clicked", lambda *_: self.view.zoom_by(1.5))
        for w in (zout, self.zoom_btn, zin):
            zoombox.append(w)
        self.zoom_widgets = (zout, self.zoom_btn, zin)
        self.before_btn = Gtk.ToggleButton(label="Vorher", tooltip_text="Ohne Filter vergleichen (nur Scanner-Kennlinie)")
        self.before_btn.connect("toggled", lambda *_: self.refresh())
        header.pack_end(zoombox)
        header.pack_end(self.before_btn)

        for name, cb in (("save-raw", lambda *_: self.save_dialog(raw=True)),
                         ("about", lambda *_: self.about()),
                         ("zoom-fit", lambda *_: self.view.zoom_fit()),
                         ("zoom-100", lambda *_: self.view.zoom_100()),
                         ("zoom-in", lambda *_: self.view.zoom_by(1.5)),
                         ("zoom-out", lambda *_: self.view.zoom_by(1 / 1.5)),
                         ("crop", lambda *_: self.start_crop()),
                         ("ocr", lambda *_: self.ocr_dialog()),
                         ("new-doc", lambda *_: self.new_document()),
                         ("crop-apply", lambda *_: self.finish_crop(True)),
                         ("crop-cancel", lambda *_: self.finish_crop(False))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        a = Gio.SimpleAction.new("zoom-to", GLib.VariantType.new("i"))
        a.connect("activate", lambda _a, v: self.view.set_zoom(v.get_int32() / 100 / self.get_scale_factor()))
        self.add_action(a)

        # Vorschau
        self.view = V.ImageView(self._render_detail)
        self.view.connect("view-changed", lambda *_: self._update_zoom_label())
        self.spinner = Adw.Spinner(halign=Gtk.Align.END, valign=Gtk.Align.START, visible=False,
                                   width_request=24, height_request=24, margin_top=12, margin_end=12,
                                   can_target=False)
        overlay = Gtk.Overlay(child=self.view)
        overlay.add_overlay(self.spinner)
        overlay.add_overlay(self._crop_bar())
        self.status = Adw.StatusPage(icon_name="scanner-symbolic", title="Noch kein Bild",
                                     description="Foto einlegen und „Scannen“ drücken\noder ein Bild öffnen.")
        self.busy = Adw.StatusPage(title="Scanne …", description="Das Blatt läuft durch den Scanner.")
        self.busy.set_child(Adw.Spinner(width_request=48, height_request=48))
        self.stack = Gtk.Stack()
        self.stack.add_named(self.status, "empty")
        self.stack.add_named(self.busy, "busy")
        self.stack.add_named(overlay, "image")
        self.info = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], margin_start=12, margin_bottom=6,
                              margin_top=4)
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        left.append(self.stack)
        left.append(self._page_strip())
        left.append(self.info)

        self.sidebar = self._sidebar()
        paned = Gtk.Paned(start_child=left, end_child=self.sidebar, shrink_end_child=False,
                          resize_end_child=False, position=1000)
        self.toasts = Adw.ToastOverlay(child=paned)
        tv = Adw.ToolbarView(content=self.toasts)
        tv.add_top_bar(header)
        self.set_content(tv)
        self._set_widgets(self.params)
        self._set_sensitive(False)
        for a in ("crop-apply", "crop-cancel"):
            self.lookup_action(a).set_enabled(False)

    def _sidebar(self):
        page = Adw.PreferencesPage(width_request=400)

        g = Adw.PreferencesGroup(title="Voreinstellung")
        self.preset_row = Adw.ComboRow(title="Stil", model=Gtk.StringList.new(list(F.PRESETS)))
        self.preset_row.connect("notify::selected", self._on_preset)
        g.add(self.preset_row)
        reset = Adw.ButtonRow(title="Auf Voreinstellung zurücksetzen")
        reset.connect("activated", lambda *_: self._on_preset())
        g.add(reset)
        page.add(g)

        g = Adw.PreferencesGroup(title="Geometrie")
        rot = Adw.ActionRow(title="Drehen")
        for icon, d in (("object-rotate-left-symbolic", -90), ("object-rotate-right-symbolic", 90)):
            b = Gtk.Button(icon_name=icon, valign=Gtk.Align.CENTER, css_classes=["flat"])
            b.connect("clicked", lambda _b, d=d: self._set("rotate", (self.params.rotate + d) % 360))
            rot.add_suffix(b)
        rot.add_suffix(self._reset_button("rotate"))
        g.add(rot)
        self._slider(g, "Begradigen", "straighten", -10, 10, 0.1, 1, "Grad")
        self.crop_row = Adw.ActionRow(title="Zuschneiden", subtitle="ganzes Bild")
        cb = Gtk.Button(label="Rahmen setzen …", valign=Gtk.Align.CENTER, action_name="win.crop",
                        tooltip_text="Zuschnitt im Bild ziehen (C)")
        self.crop_row.add_suffix(cb)
        self.crop_row.add_suffix(self._reset_button("crop"))
        self._setters["crop"] = lambda v: self.crop_row.set_subtitle(
            "von Hand gesetzt" if v else ("automatisch" if self.params.auto_crop else "ganzes Bild"))
        g.add(self.crop_row)
        self._switch(g, "Automatisch auf das Foto zuschneiden", "auto_crop",
                     "dunklen Scannerhintergrund entfernen")
        self._switch(g, "Papierrand mit entfernen", "trim_border",
                     "weissen oder cremefarbenen Rand alter Abzüge abschneiden")
        self._slider(g, "Rand nach innen", "crop_margin", 0, 100, 1, 0, "Pixel, beim automatischen Zuschnitt")
        page.add(g)

        g = Adw.PreferencesGroup(title="Ton &amp; Farbe")
        self._switch(g, "Scanner-Kennlinie", "scanner_curve", "für Rohscans an — wie die Windows-Software")
        self._slider(g, "Automatischer Tonwert", "auto_levels", 0, 1, 0.05, 2)
        self._slider(g, "Helligkeit", "brightness", -1, 1, 0.02, 2)
        self._slider(g, "Kontrast", "contrast", -1, 1, 0.02, 2)
        self._slider(g, "Gamma", "gamma", 0.5, 2, 0.02, 2)
        self._slider(g, "Farbtemperatur", "temperature", -1, 1, 0.02, 2, "kalt ↔ warm")
        self._slider(g, "Tönung", "tint", -1, 1, 0.02, 2, "grün ↔ magenta")
        self._slider(g, "Sättigung", "saturation", 0, 2, 0.02, 2)
        modes = Adw.ComboRow(title="Farbmodus", model=Gtk.StringList.new(["Farbe", "Schwarzweiss", "Sepia"]))
        modes.connect("notify::selected", lambda r, _p: self._set("color_mode", F.COLOR_MODES[r.get_selected()]))
        self._setters["color_mode"] = lambda v, r=modes: r.set_selected(F.COLOR_MODES.index(v))
        modes.add_suffix(self._reset_button("color_mode"))
        g.add(modes)
        page.add(g)

        g = Adw.PreferencesGroup(title="Foto-Restaurierung")
        self._switch(g, "Treppen korrigieren", "dejitter", "Zeilenversatz des Scanners")
        set_sw = self._setters["dejitter"]

        def set_dejitter(v, set_sw=set_sw):
            set_sw(v)
            row = set_sw.__self__.get_ancestor(Adw.ActionRow)
            if self.full is None or not hasattr(self, "full_src"):
                return
            row.set_subtitle("Zeilenversatz erkannt und ausgeglichen" if self.full_dj is not None and v else
                             "Zeilenversatz erkannt — nicht ausgeglichen" if self.full_dj is not None else
                             "kein Zeilenversatz im Bild — nichts zu tun")
        self._setters["dejitter"] = set_dejitter
        self._slider(g, "Verblassung ausgleichen", "fade_restore", 0, 1, 0.05, 2, "Farbstich alter Abzüge")
        self._slider(g, "Lokaler Kontrast", "local_contrast", 0, 1, 0.05, 2)
        self._slider(g, "Staub &amp; Kratzer", "dust", 0, 1, 0.05, 2)
        page.add(g)

        g = Adw.PreferencesGroup(title="Schärfe &amp; Rauschen",
                                 description="Wirkt auf feinste Details — zum Beurteilen hineinzoomen "
                                             "(Mausrad, Doppelklick = 100 %).")
        self._slider(g, "Entrauschen", "denoise", 0, 1, 0.05, 2)
        self._slider(g, "Filmkorn glätten", "grain", 0, 1, 0.05, 2)
        self._slider(g, "Nachschärfen", "sharpen", 0, 3, 0.05, 2)
        self._slider(g, "Schärferadius", "sharpen_radius", 0.5, 3, 0.1, 1, "Pixel")
        page.add(g)
        return page

    def _slider(self, group, title, field, lo, hi, step, digits, subtitle=None):
        row = Adw.ActionRow(title=title, subtitle=subtitle or "")
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        sc.set_digits(digits)
        sc.set_draw_value(True)
        sc.set_value_pos(Gtk.PositionType.RIGHT)
        sc.set_size_request(190, -1)
        sc.set_valign(Gtk.Align.CENTER)
        # Füllung nur bei Reglern, die bei 0 beginnen; sonst Marke beim Neutralwert
        sc.set_has_origin(lo == 0)
        if lo != 0:
            sc.add_mark(getattr(F.Params(), field), Gtk.PositionType.BOTTOM, None)
        is_int = isinstance(getattr(F.Params(), field), int)
        sc.connect("value-changed", lambda s: self._set(field, int(round(s.get_value())) if is_int
                                                        else round(s.get_value(), 4)))
        # Mausrad verstellt den Regler nicht, sondern scrollt die Seitenleiste —
        # sonst ändert man beim Durchscrollen versehentlich Werte
        wheel = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        wheel.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        wheel.connect("scroll", self._scroll_sidebar)
        sc.add_controller(wheel)
        row.add_suffix(sc)
        row.add_suffix(self._reset_button(field))
        self._setters[field] = sc.set_value
        group.add(row)

    def _scroll_sidebar(self, ctrl, _dx, dy):
        sw = ctrl.get_widget().get_ancestor(Gtk.ScrolledWindow)
        if sw is None:
            return True
        adj = sw.get_vadjustment()
        # Mausrad: Schrittweite wie GTK selbst (Seitenhöhe hoch 2/3); Touchpad: Pixel
        step = adj.get_page_size() ** (2 / 3) if ctrl.get_unit() == Gdk.ScrollUnit.WHEEL else 1.0
        adj.set_value(min(max(adj.get_value() + dy * step, adj.get_lower()),
                          adj.get_upper() - adj.get_page_size()))
        return True

    def _switch(self, group, title, field, subtitle=None):
        row = Adw.ActionRow(title=title, subtitle=subtitle or "")
        sw = Gtk.Switch(valign=Gtk.Align.CENTER)
        sw.connect("notify::active", lambda w, _p: self._set(field, w.get_active()))
        row.add_suffix(sw)
        row.add_suffix(self._reset_button(field))
        row.set_activatable_widget(sw)
        self._setters[field] = sw.set_active
        group.add(row)

    def _reset_button(self, field):
        """Knopf, der einen einzelnen Wert auf die Voreinstellung zurücksetzt."""
        b = Gtk.Button(icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                       tooltip_text="Zurücksetzen", sensitive=False)
        b.connect("clicked", lambda *_: self._set(field, getattr(self._preset(), field), sync=True))
        self._reset_btns[field] = b
        return b

    def _update_resets(self):
        pre = self._preset()
        for f, b in self._reset_btns.items():
            b.set_sensitive(getattr(self.params, f) != getattr(pre, f))

    # ---- Parameter -----------------------------------------------------------
    def _preset(self):
        return list(F.PRESETS.values())[self.preset_row.get_selected()]

    def _on_preset(self, *_):
        p = replace(self._preset(), rotate=self.params.rotate, straighten=self.params.straighten)
        if not self.params.scanner_curve and self._is_processed_file:
            p = replace(p, scanner_curve=False)
        self._set_widgets(p)
        self.params = p
        self.refresh()

    def _set_widgets(self, p):
        self._updating = True
        for k, setter in self._setters.items():
            setter(getattr(p, k))
        self._updating = False
        self._update_resets()

    def _set(self, field, value, sync=False):
        if self._updating or getattr(self.params, field) == value:
            return
        self.params = replace(self.params, **{field: value})
        if sync or field == "rotate":
            self._set_widgets(self.params)
        self._update_resets()
        if field == "auto_crop":
            self._setters["crop"](self.params.crop)
        if field == "dejitter" and self.full is not None:
            self._select_source()
            self._setters["dejitter"](value)
            self.view.detail = None
        if (field in DETAIL_FIELDS and self.view.zoom_percent() < 60 and self.full is not None
                and not self._detail_hint_shown):
            self._detail_hint_shown = True
            self.toast("Diesen Filter sieht man erst hineingezoomt — Doppelklick ins Bild für 100 %")
        self.refresh()

    # ---- Bild laden ------------------------------------------------------------
    _is_processed_file = False

    def set_image(self, rgb, dpi, name, processed=False):
        """Neues Bild.  Die Treppen-Korrektur hängt nicht von den Reglern ab und
        wird deshalb einmal je Bild im Hintergrund gerechnet."""
        rgb = np.ascontiguousarray(rgb)
        self.spinner.set_visible(True)

        def job():
            return F.dejitter(rgb, linear=not processed)

        def done(res, err):
            self.spinner.set_visible(False)
            if err:
                self.toast(f"Treppen-Korrektur fehlgeschlagen: {err}")
                res = (rgb, None, 0.0, 0.0)
            self._use_image(rgb, res, dpi, name, processed)
            return False

        self.worker.submit(job, done)

    def _use_image(self, rgb, jitter, dpi, name, processed):
        """Neues Bild als Seite übernehmen — angehängt im Mehrseiten-Modus, sonst als neues Dokument."""
        dj, _offs, before, _after = jitter
        if self.cropping:
            self.finish_crop(False)
        # Einstellungen der bisherigen Seite weiterverwenden (praktisch bei Dokumenten),
        # nur Zuschnitt nicht; Rohscans brauchen die Scanner-Kennlinie, fertige Bilder nicht
        base = replace(self.params, crop=None, scanner_curve=not processed)
        if processed and self.params.scanner_curve:
            self.toast("Bild ist bereits aufbereitet — Scanner-Kennlinie ausgeschaltet")
        page = Page(rgb, dj if dj is not rgb else None, before, dpi, name, processed, base)
        if self.multipage and self.pages:
            self._store_current()
            self.pages.append(page)
        else:
            self.pages = [page]
        self._activate(len(self.pages) - 1, store=False)

    def _store_current(self):
        if 0 <= self.cur < len(self.pages):
            self.pages[self.cur].params = self.params

    def _activate(self, i, store=True):
        """Seite i anzeigen und bearbeiten."""
        if store:
            self._store_current()
        if self.cropping:
            self.finish_crop(False)
        self.cur = i
        pg = self.pages[i]
        self.full_src, self.full_dj, self._jitter_before = pg.src, pg.dj, pg.before
        self.dpi, self.source_name, self._is_processed_file = pg.dpi, pg.name, pg.processed
        self.params = pg.params
        self._proxies = pg.proxies
        self._geom_cache = None
        self._select_source()
        self._set_widgets(self.params)
        self._setters["dejitter"](self.params.dejitter)
        self.stack.set_visible_child_name("image")
        self._set_sensitive(True)
        self._rebuild_strip()
        self.refresh()

    def _select_source(self):
        """Vollbild und Vorschau je nach Schalter „Treppen korrigieren“ wählen."""
        use_dj = self.params.dejitter and self.full_dj is not None
        self.full = self.full_dj if use_dj else self.full_src
        self._geom_cache = None
        if use_dj not in self._proxies:
            s = min(1.0, PREVIEW_MAX / max(self.full.shape[:2]))
            self._proxies[use_dj] = (s, cv2.resize(self.full, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
                                     if s < 1 else self.full)
        self.proxy_scale, self.proxy = self._proxies[use_dj]

    def load_file(self, path):
        try:
            im = Image.open(path)
            dpi = int(round(im.info.get("dpi", (self.dpi, self.dpi))[0])) or 600
            rgb = np.asarray(im.convert("RGB"))
        except Exception as e:  # noqa: BLE001
            self.toast(f"Kann {Path(path).name} nicht öffnen: {e}")
            return
        # lineare Rohdaten sind sehr dunkel (Median ~80); aufbereitete Bilder nicht
        processed = float(np.median(rgb[::8, ::8])) > 110
        self.set_image(rgb, dpi, Path(path).stem, processed)
        self._dirty = False              # liegt ja als Datei vor

    def _set_sensitive(self, on):
        for w in (self.save_btn, self.before_btn, *self.zoom_widgets):
            w.set_sensitive(on)
        for a in ("save-raw", "crop", "ocr", "zoom-fit", "zoom-100", "zoom-in", "zoom-out", "zoom-to"):
            self.lookup_action(a).set_enabled(on)

    # ---- Vorschau ---------------------------------------------------------------
    def refresh(self):
        if self.proxy is None:
            return
        if self._pending:
            GLib.source_remove(self._pending)
        self._pending = GLib.timeout_add(80, self._start_render)   # Regler entprellen

    def _view_params(self):
        p = self.params
        if self.before_btn.get_active():
            p = F.Params(rotate=p.rotate, straighten=p.straighten, auto_crop=p.auto_crop,
                         crop_margin=p.crop_margin, crop=p.crop, scanner_curve=p.scanner_curve, sharpen=0.0)
        # beim Zuschneiden das ganze Bild zeigen, der Rahmen liegt darüber
        return p.uncropped() if self.cropping else p

    def _geometry_full(self, p, stats):
        """Geometrisch bearbeitetes Vollbild, zwischengespeichert (Drehen kostet bei 600 dpi spürbar)."""
        key = (p.rotate, p.straighten, p.auto_crop, p.crop_margin, p.crop)
        cache = self._geom_cache
        if not cache or cache[0] != key:
            cache = (key, F.geometry(self.full, p, stats, 1.0))
            self._geom_cache = cache
        return cache[1]

    def _start_render(self):
        self._pending = None
        p, proxy, scale, page = self._view_params(), self.proxy, self.proxy_scale, self.cur
        self.spinner.set_visible(True)

        def job():
            t0 = time.time()
            stats = {}
            prev = F.process(proxy, p, scale, stats)
            g = self._geometry_full(p, stats)
            th = 96 / prev.shape[0]
            thumb = cv2.resize(prev, None, fx=th, fy=th, interpolation=cv2.INTER_AREA)
            return prev, (g.shape[1], g.shape[0]), p, stats, time.time() - t0, page, thumb

        self.worker.submit(job, self._render_done)
        return False

    def _render_done(self, res, err):
        self.spinner.set_visible(False)
        if err:
            self.toast(f"Fehler in der Bearbeitung: {err}")
            return False
        prev, size, p, stats, dt, page, thumb = res
        if page != self.cur:                # inzwischen andere Seite gewählt
            return False
        self._last = (p, stats)
        if not self.cropping and not self.before_btn.get_active() and 0 <= page < len(self.pages):
            self.pages[page].thumb = to_texture(thumb)
            self._update_thumb(page)
        self.view.set_base(to_texture(prev), size, prev.shape[1] / size[0])
        if self.cropping and self.view.crop is None:
            self.view.begin_crop(self._crop_start)
        self._render_time = dt
        self._update_zoom_label()
        if self.on_preview_done:
            self.on_preview_done()
        return False

    def _render_detail(self, rect, scale, token):
        """Vom Betrachter angefordert: sichtbaren Ausschnitt in Bildschirmauflösung rechnen."""
        if not self._last:
            return
        p, stats = self._last
        x, y, w, h = rect
        self.spinner.set_visible(True)

        def job():
            g = self._geometry_full(p, dict(stats))
            part = g[y:y + h, x:x + w]
            if scale < 0.999:
                part = cv2.resize(part, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            return F.process(part, p, scale, dict(stats), geometry_done=True)

        def done(img, err):
            self.spinner.set_visible(False)
            if err:
                self.toast(f"Fehler in der Bearbeitung: {err}")
            elif self._last and self._last[0] == p:
                self.view.deliver_detail(token, to_texture(img), (x, y, w, h, scale))
            return False

        self.detail_worker.submit(job, done)

    def _update_zoom_label(self):
        self.zoom_btn.set_label(V.zoom_label(self.view))
        if self.full is None:
            return
        h, w = self.full.shape[:2]
        out = f"{self.view.iw}×{self.view.ih}" if (self.view.iw, self.view.ih) != (w, h) else ""
        self.info.set_text(
            f"{self.source_name} — {w}×{h} px, {self.dpi} dpi ({w / self.dpi * 25.4:.0f}×{h / self.dpi * 25.4:.0f} mm)"
            + (f" · Ergebnis {out} px ({self.view.iw / self.dpi * 25.4:.0f}×{self.view.ih / self.dpi * 25.4:.0f} mm)"
               if out and not self.cropping else "")
            + f" · Zoom {V.zoom_label(self.view)} · {getattr(self, '_render_time', 0) * 1000:.0f} ms"
            + (" · VORHER" if self.before_btn.get_active() else "")
            + (" · ZUSCHNEIDEN: Rahmen ziehen, Enter übernimmt, Esc bricht ab" if self.cropping else ""))

    # ---- Seiten -------------------------------------------------------------------
    def _page_strip(self):
        self.strip_box = Gtk.Box(spacing=6, margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
        sc = Gtk.ScrolledWindow(child=self.strip_box, hexpand=True, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                min_content_height=128)
        tools = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER, margin_end=8)
        row1, row2 = Gtk.Box(css_classes=["linked"]), Gtk.Box(spacing=4)
        self.strip_btns = {}
        for key, icon, tip, cb in (
                ("left", "go-previous-symbolic", "Seite nach vorne", lambda *_: self._move_page(-1)),
                ("right", "go-next-symbolic", "Seite nach hinten", lambda *_: self._move_page(1)),
                ("delete", "user-trash-symbolic", "Seite entfernen", lambda *_: self._delete_page())):
            b = Gtk.Button(icon_name=icon, tooltip_text=tip)
            b.connect("clicked", cb)
            self.strip_btns[key] = b
            (row1 if key != "delete" else row2).append(b)
        row2.prepend(row1)
        allb = Gtk.Button(label="Auf alle Seiten", tooltip_text="Einstellungen dieser Seite auf alle Seiten "
                                                                 "übertragen (ohne Zuschnitt)")
        allb.connect("clicked", lambda *_: self._apply_to_all())
        self.strip_btns["all"] = allb
        tools.append(row2)
        tools.append(allb)
        bar = Gtk.Box(css_classes=["view"])
        bar.append(sc)
        bar.append(tools)
        self.strip = Gtk.Revealer(child=bar, reveal_child=False,
                                  transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        return self.strip

    def _rebuild_strip(self):
        while (c := self.strip_box.get_first_child()):
            self.strip_box.remove(c)
        self._thumb_pics = []
        group = None
        for i, pg in enumerate(self.pages):
            pic = Gtk.Picture(paintable=pg.thumb, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN,
                              width_request=72, height_request=96)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(pic)
            box.append(Gtk.Label(label=str(i + 1), css_classes=["caption"]))
            b = Gtk.ToggleButton(child=box, css_classes=["flat"], active=i == self.cur,
                                 tooltip_text=f"Seite {i + 1}: {pg.name}")
            if group:
                b.set_group(group)
            group = group or b
            b.connect("toggled", lambda b, i=i: b.get_active() and i != self.cur and self._activate(i))
            self.strip_box.append(b)
            self._thumb_pics.append(pic)
        add = Gtk.Button(tooltip_text="Weitere Seite scannen", valign=Gtk.Align.CENTER, css_classes=["flat"])
        add.set_child(Gtk.Image(icon_name="list-add-symbolic", pixel_size=32))
        add.connect("clicked", lambda *_: self._scan_more())
        self.strip_box.append(add)
        n = len(self.pages)
        self.strip.set_reveal_child(n > 1 or (self.multipage and n > 0))
        self.strip_btns["left"].set_sensitive(self.cur > 0)
        self.strip_btns["right"].set_sensitive(0 <= self.cur < n - 1)
        self.strip_btns["delete"].set_sensitive(n > 0)
        self.strip_btns["all"].set_sensitive(n > 1)

    def _update_thumb(self, i):
        if i < len(getattr(self, "_thumb_pics", [])):
            self._thumb_pics[i].set_paintable(self.pages[i].thumb)

    def _scan_more(self):
        if not self.multipage:
            self.lookup_action("multipage").change_state(GLib.Variant("b", True))
        self.scan()

    def _move_page(self, d):
        j = self.cur + d
        if 0 <= j < len(self.pages):
            self._store_current()
            self.pages[self.cur], self.pages[j] = self.pages[j], self.pages[self.cur]
            self.cur = j
            self._rebuild_strip()

    def _delete_page(self):
        if not self.pages:
            return
        self._store_current()
        removed, idx = self.pages.pop(self.cur), self.cur
        t = Adw.Toast(title=f"Seite {idx + 1} entfernt", button_label="Rückgängig", timeout=8)

        def undo(*_):
            self._store_current()
            self.pages.insert(idx, removed)
            self._activate(idx, store=False)
        t.connect("button-clicked", undo)
        self.toasts.add_toast(t)
        if self.pages:
            self._activate(min(idx, len(self.pages) - 1), store=False)
        else:
            self._clear_document()

    def _apply_to_all(self):
        self._store_current()
        for pg in self.pages:
            pg.params = replace(self.params, crop=pg.params.crop, scanner_curve=not pg.processed)
        self.toast(f"Einstellungen auf alle {len(self.pages)} Seiten übertragen")
        for i, pg in enumerate(self.pages):    # Vorschaubilder der anderen Seiten sind jetzt veraltet
            if i != self.cur:
                pg.thumb = None
        self._rebuild_strip()

    def _on_multipage(self, action, value):
        action.set_state(value)
        self.multipage = value.get_boolean()
        self.settings["multipage"] = self.multipage
        self._save_settings()
        self._rebuild_strip()
        if self.multipage:
            self.toast("Mehrseitig: jeder Scan wird als weitere Seite angehängt")

    def new_document(self):
        if self._dirty and self.pages:
            dlg = Adw.AlertDialog(heading="Neues Dokument?",
                                  body=f"Das aktuelle Dokument ({len(self.pages)} Seite(n)) ist nicht gespeichert.")
            dlg.add_response("cancel", "Abbrechen")
            dlg.add_response("discard", "Verwerfen")
            dlg.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.connect("response", lambda _d, r: r == "discard" and self._clear_document())
            dlg.present(self)
        else:
            self._clear_document()

    def _clear_document(self):
        if self.cropping:
            self.finish_crop(False)
        self.pages, self.cur = [], -1
        self.full = self.proxy = None
        self._last = None
        self._dirty = False
        self.view.clear()
        self.stack.set_visible_child_name("empty")
        self._set_sensitive(False)
        self._rebuild_strip()
        self.info.set_text("")

    # ---- Texterkennung ---------------------------------------------------------------
    def ocr_dialog(self):
        if self.full is None:
            return
        langs = X.tesseract_langs()
        if not langs:
            self.toast("Texterkennung nicht installiert: sudo apt install tesseract-ocr tesseract-ocr-deu")
            return
        self._store_current()
        lang = self.settings.get("ocr_lang") or ("deu+eng" if {"deu", "eng"} <= set(langs) else langs[0])
        page = self.pages[self.cur]
        self.spinner.set_visible(True)
        self.title.set_subtitle("Texterkennung …")

        def job():
            return X.ocr_text(X.render_page(page), lang, page.dpi)

        def done(text, err):
            self.spinner.set_visible(False)
            self.title.set_subtitle("")
            if err:
                self.toast(str(err))
                return False
            view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=False,
                                top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
            view.get_buffer().set_text(text.strip() or "(kein Text erkannt)")
            sc = Gtk.ScrolledWindow(child=view, vexpand=True)
            copy = Gtk.Button(label="Kopieren", css_classes=["suggested-action"])

            def do_copy(*_):
                self.get_clipboard().set(text)
                self.toast("Text in die Zwischenablage kopiert")
            copy.connect("clicked", do_copy)
            hb = Adw.HeaderBar()
            hb.pack_end(copy)
            tv = Adw.ToolbarView(content=sc)
            tv.add_top_bar(hb)
            Adw.Dialog(title=f"Erkannter Text — Seite {self.cur + 1}", child=tv,
                       content_width=640, content_height=720).present(self)
            return False

        self.worker.submit(job, done)

    # ---- Zuschneiden ------------------------------------------------------------
    def _crop_bar(self):
        bar = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER, valign=Gtk.Align.END, margin_bottom=18,
                      css_classes=["toolbar", "osd"], visible=False)
        self.aspect_dd = Gtk.DropDown.new_from_strings([n for n, _r in ASPECTS])
        self.aspect_dd.set_tooltip_text("Seitenverhältnis")
        self.aspect_dd.connect("notify::selected", lambda *_: self._apply_aspect())
        flip = Gtk.Button(icon_name="object-flip-horizontal-symbolic", tooltip_text="Hoch-/Querformat tauschen")
        flip.connect("clicked", lambda *_: self._apply_aspect(flip=True))
        full = Gtk.Button(label="Ganzes Bild", tooltip_text="Rahmen auf das ganze Bild setzen")
        full.connect("clicked", lambda *_: (self.view.begin_crop(None), self._apply_aspect()))
        cancel = Gtk.Button(label="Abbrechen", action_name="win.crop-cancel")
        ok = Gtk.Button(label="Übernehmen", action_name="win.crop-apply", css_classes=["suggested-action"])
        for w in (self.aspect_dd, flip, full, cancel, ok):
            bar.append(w)
        self._portrait = None
        self.crop_bar = bar
        return bar

    def _apply_aspect(self, flip=False):
        if not self.cropping or not self.view.crop:
            return
        r = ASPECTS[self.aspect_dd.get_selected()][1]
        x0, y0, x1, y1 = self.view.crop
        if self._portrait is None or not flip and r is None:
            self._portrait = (y1 - y0) > (x1 - x0)
        if flip:
            self._portrait = not self._portrait
        if r == "orig":
            ratio = self.view.iw / self.view.ih
            r = max(ratio, 1 / ratio)
        if r is None:
            self.view.set_aspect(None)
            if flip:                       # frei: Rahmen um die Mitte drehen
                cx, cy, w, h = (x0 + x1) / 2, (y0 + y1) / 2, x1 - x0, y1 - y0
                self.view.crop = self.view._clamp_rect([cx - h / 2, cy - w / 2, cx + h / 2, cy + w / 2])
                self.view.queue_draw()
            return
        self.view.set_aspect(1 / r if self._portrait else r)

    def start_crop(self):
        if self.full is None or self.cropping:
            return
        self.cropping = True
        self._crop_before = (self.params.crop, self.params.auto_crop)
        # Startrahmen: bisheriger Zuschnitt, sonst das automatisch erkannte Foto, sonst alles
        self._crop_start = None
        g = self._geometry_full(self.params.uncropped(), {})
        if self.params.crop:
            x0, y0, x1, y1 = self.params.crop
        else:
            x0, y0, x1, y1 = F.find_photo(g, self.params.trim_border) or (0, 0, 1, 1)
        self._crop_start = (x0 * g.shape[1], y0 * g.shape[0], x1 * g.shape[1], y1 * g.shape[0])
        self._portrait = None
        self.aspect_dd.set_selected(0)
        self.view.set_aspect(None)
        self.view.crop = None
        self.crop_bar.set_visible(True)
        self._set_sidebar_sensitive(False)
        for a in ("crop-apply", "crop-cancel"):
            self.lookup_action(a).set_enabled(True)
        self.refresh()

    def finish_crop(self, apply):
        if not self.cropping:
            return
        rel = self.view.crop_relative() if (apply and self.view.crop) else None
        self.cropping = False
        self.view.end_crop()
        self.crop_bar.set_visible(False)
        self._set_sidebar_sensitive(True)
        for a in ("crop-apply", "crop-cancel"):
            self.lookup_action(a).set_enabled(False)
        if apply and rel:
            full_frame = rel[0] <= 0.001 and rel[1] <= 0.001 and rel[2] >= 0.999 and rel[3] >= 0.999
            self.params = replace(self.params, crop=None if full_frame else rel,
                                  auto_crop=False if not full_frame else self.params.auto_crop)
            self._set_widgets(self.params)
        self.refresh()

    def _set_sidebar_sensitive(self, on):
        self.sidebar.set_sensitive(on)
        for w in (self.scan_btn, self.save_btn, self.before_btn):
            w.set_sensitive(on)

    # ---- Scannen -----------------------------------------------------------------
    def _set_scan_mode(self, mode):
        auto = mode == "auto"
        self.lookup_action("scan-mode").set_state(GLib.Variant("s", "auto" if auto else "manual"))
        self.settings["scan_mode"] = "auto" if auto else "manual"
        self._save_settings()
        self.scan_btn.set_label("Automatisch" if auto else "Scannen")
        self.scan_btn.set_tooltip_text("Scan startet, sobald ein Foto eingelegt ist — Klick scannt sofort"
                                       if auto else "Jetzt scannen")
        self.status.set_description("Foto einlegen — der Scan startet automatisch.\nOder ein Bild öffnen."
                                    if auto else "Foto einlegen und „Scannen“ drücken\noder ein Bild öffnen.")
        self.watcher.set_armed(auto)
        if not auto:
            self.title.set_subtitle("")

    def _watch_state(self, text):
        self.title.set_subtitle(text or "")
        return False

    def _auto_scan(self):
        """Vom Papiersensor ausgelöst."""
        if not self.scan_btn.get_sensitive():       # läuft schon ein Scan
            self.watcher.resume()
            return False
        if self.cropping:
            self.finish_crop(False)
        self.scan(auto=True)
        return False

    def scan(self, auto=False):
        dpi = 600 if self.res_dd.get_selected() == 1 else 300
        self.settings["dpi"] = dpi
        self._save_settings()
        self.scan_btn.set_sensitive(False)
        self.stack.set_visible_child_name("busy")
        if not auto:
            self.watcher.pause()         # Sensorabfrage nicht zwischen die Scan-Transfers
        # bisheriges Dokument merken, falls es ersetzt wird und noch nicht gespeichert war
        self._previous = self._snapshot_state() if (self._dirty and not self.multipage) else None

        def job():
            out = Path(tempfile.mkstemp(suffix=".pnm", prefix="ephoto-")[1])
            base = ["scanimage", "-d", DEVICE, "--resolution", str(dpi), "--format=pnm", "-o", str(out)]
            r = subprocess.run(base[:5] + ["--enhance=no"] + base[5:], capture_output=True, text=True)
            if r.returncode and "enhance" in r.stderr:      # älteres Backend ohne die Option: liefert linear
                r = subprocess.run(base, capture_output=True, text=True)
            try:
                if r.returncode:
                    raise RuntimeError(r.stderr.strip() or f"scanimage Rückgabewert {r.returncode}")
                return np.asarray(Image.open(out).convert("RGB")), dpi
            finally:
                out.unlink(missing_ok=True)

        self.worker.submit(job, self._scan_done)

    def _scan_done(self, res, err):
        self.scan_btn.set_sensitive(True)
        # nach dem Scan erst wieder auslösen, wenn das Foto heraus ist
        self.watcher.resume(wait_for_removal=not err)
        if err:
            msg = str(err)
            if "no more documents" in msg.lower() or "Document feeder out of documents" in msg:
                msg = "Kein Blatt eingelegt."
            elif "no SANE devices" in msg or "Invalid argument" in msg:
                msg = "Scanner nicht gefunden — angeschlossen und nicht an die VM durchgereicht?"
            self.stack.set_visible_child_name("image" if self.full is not None else "empty")
            self.toast(f"Scan fehlgeschlagen: {msg}")
            return False
        rgb, dpi = res
        self.set_image(rgb, dpi, time.strftime("scan_%Y-%m-%d_%H%M%S"))
        if self._previous:
            prev = self._previous
            t = Adw.Toast(title=f"„{prev['name']}“ war nicht gespeichert", button_label="Zurückholen", timeout=10)
            t.connect("button-clicked", lambda *_: self._restore_state(prev))
            self.toasts.add_toast(t)
        self._dirty = True               # frischer Scan liegt nur im Speicher
        return False

    # ---- Speichern -------------------------------------------------------------
    def open_dialog(self):
        d = Gtk.FileDialog(title="Bild öffnen")
        f = Gtk.FileFilter(name="Bilder")
        for m in ("image/png", "image/jpeg", "image/tiff", "image/x-portable-anymap"):
            f.add_mime_type(m)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        d.set_filters(store)
        d.open(self, None, lambda d, r: self._opened(d, r))

    def _opened(self, d, r):
        try:
            f = d.open_finish(r)
        except GLib.Error:
            return
        self.load_file(f.get_path())

    def save_dialog(self, raw=False):
        if self.full is None:
            return
        self._store_current()
        if raw:
            self._file_dialog("Rohscan speichern", f"{self.source_name}_roh.png",
                              lambda path: self._export(path, "raw", [self.pages[self.cur]], None, 0))
            return
        n = len(self.pages)
        langs = X.tesseract_langs()
        group = Adw.PreferencesGroup()
        fmt_row = Adw.ComboRow(title="Format", model=Gtk.StringList.new([f[1] for f in X.FORMATS]))
        keys = [f[0] for f in X.FORMATS]
        last = self.settings.get("format_multi" if n > 1 else "format", "pdf" if n > 1 else "jpg")
        fmt_row.set_selected(keys.index(last) if last in keys else 0)
        group.add(fmt_row)
        pages_row = Adw.ComboRow(title="Seiten", model=Gtk.StringList.new([f"Alle {n} Seiten", "Nur aktuelle Seite"]),
                                 visible=n > 1)
        group.add(pages_row)
        lang_codes = langs or []
        lang_row = Adw.ComboRow(title="Sprache der Texterkennung")
        if langs:
            combos = (["deu+eng"] if {"deu", "eng"} <= set(langs) else []) + langs
            lang_codes = combos
            lang_row.set_model(Gtk.StringList.new(
                ["Deutsch + Englisch" if c == "deu+eng" else X.LANG_NAMES.get(c, c) for c in combos]))
            prev_lang = self.settings.get("ocr_lang", "deu+eng" if "deu+eng" in combos else combos[0])
            lang_row.set_selected(combos.index(prev_lang) if prev_lang in combos else 0)
        else:
            lang_row.set_model(Gtk.StringList.new(["—"]))
            lang_row.set_subtitle("Texterkennung nicht installiert: sudo apt install tesseract-ocr tesseract-ocr-deu")
            lang_row.set_sensitive(False)
        group.add(lang_row)
        quality = Adw.SpinRow.new_with_range(50, 100, 1)
        quality.set_title("JPEG-Qualität")
        quality.set_value(self.settings.get("quality", 90))
        group.add(quality)

        def sync(*_):
            key = keys[fmt_row.get_selected()]
            needs_ocr = X.FORMAT_BY_KEY[key][3]
            lang_row.set_visible(needs_ocr)
            quality.set_visible(key in ("pdf", "pdf-plain", "jpg"))
            fmt_row.set_subtitle("Texterkennung fehlt — bitte installieren" if needs_ocr and not langs else "")
        fmt_row.connect("notify::selected", sync)
        sync()

        dlg = Adw.AlertDialog(heading="Speichern", extra_child=group)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("save", "Speichern …")
        dlg.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("save")

        def on_response(_d, resp):
            if resp != "save":
                return
            key = keys[fmt_row.get_selected()]
            if X.FORMAT_BY_KEY[key][3] and not langs:
                self.toast("Für dieses Format fehlt die Texterkennung: sudo apt install tesseract-ocr tesseract-ocr-deu")
                return
            pages = self.pages if pages_row.get_selected() == 0 else [self.pages[self.cur]]
            lang = lang_codes[lang_row.get_selected()] if langs else None
            q = int(quality.get_value())
            self.settings.update({"format_multi" if n > 1 else "format": key, "quality": q})
            if lang:
                self.settings["ocr_lang"] = lang
            self._save_settings()
            name = self.pages[0].name if len(pages) > 1 else self.source_name
            self._file_dialog("Speichern", name + X.FORMAT_BY_KEY[key][2],
                              lambda path: self._export(path, key, pages, lang, q))
        dlg.connect("response", on_response)
        dlg.present(self)

    def _file_dialog(self, title, initial_name, then):
        d = Gtk.FileDialog(title=title, initial_name=initial_name)
        folder = self.settings.get("folder")
        if folder and Path(folder).is_dir():
            d.set_initial_folder(Gio.File.new_for_path(folder))

        def done(d, r):
            try:
                path = Path(d.save_finish(r).get_path())
            except GLib.Error:
                return
            self.settings["folder"] = str(path.parent)
            self._save_settings()
            then(path)
        d.save(self, None, done)

    def _export(self, path, key, pages, lang, quality):
        self.spinner.set_visible(True)
        self.save_btn.set_sensitive(False)
        status = lambda m: GLib.idle_add(self.title.set_subtitle, m)   # noqa: E731

        def job():
            if key == "raw":
                pg = pages[0]
                Image.fromarray(pg.src).save(path.with_suffix(".png"), dpi=(pg.dpi, pg.dpi))
                return [path.with_suffix(".png")]
            return X.export(pages, path, key, lang=lang or "deu", quality=quality, progress=status)

        self.worker.submit(job, self._save_done)

    def _snapshot_state(self):
        self._store_current()
        name = self.pages[self.cur].name if self.pages else ""
        return {"pages": list(self.pages), "cur": self.cur, "name": name}

    def _restore_state(self, st):
        self.pages = st["pages"]
        self._activate(st["cur"], store=False)
        self._dirty = True

    def _save_done(self, res, err):
        if not err:
            self._dirty = False
        self.spinner.set_visible(False)
        self.save_btn.set_sensitive(True)
        self.title.set_subtitle("")
        if err:
            self.toast(f"Speichern fehlgeschlagen: {err}")
        else:
            self.toast(f"Gespeichert: {res[0].name}" + (f" und {len(res) - 1} weitere" if len(res) > 1 else ""))
        self.refresh()
        return False

    # ---- Sonstiges ----------------------------------------------------------------
    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def about(self):
        Adw.AboutDialog(application_name="ePhoto Scan", version="1.0", developer_name="bosstschopp-star",
                        comments="Scannen und Nachbearbeiten mit dem Plustek ePhoto Z300",
                        application_icon="scanner").present(self)

    def _load_settings(self):
        try:
            self.settings = json.loads(CONFIG.read_text())
        except (OSError, ValueError):
            self.settings = {}

    def _save_settings(self):
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps(self.settings, indent=2))
        except OSError:
            pass


class App(Adw.Application):
    def __init__(self, argv):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.argv = argv

    def do_activate(self):
        win = Window(self)
        self.set_accels_for_action("win.save-raw", ["<Shift><Control>s"])
        for name, key, fn in (("open", "<Control>o", win.open_dialog), ("save", "<Control>s", win.save_dialog)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda *_a, fn=fn: fn())
            win.add_action(a)
            self.set_accels_for_action(f"win.{name}", [key])
        for action, keys in (("win.zoom-in", ["plus", "KP_Add", "equal"]), ("win.zoom-out", ["minus", "KP_Subtract"]),
                             ("win.zoom-fit", ["0", "KP_0"]), ("win.zoom-100", ["1", "KP_1"]),
                             ("win.crop", ["c"]), ("win.crop-apply", ["Return", "KP_Enter"]),
                             ("win.crop-cancel", ["Escape"])):
            self.set_accels_for_action(action, keys)
        win.present()
        if self.argv[:1] == ["--selftest"]:
            self._selftest(win, *self.argv[1:])
        elif self.argv:
            win.load_file(self.argv[0])


    def _selftest(self, win, image, shot, preset="0", mode="0"):
        """Bild laden, Voreinstellung wählen, Fenster als PNG ablegen, beenden.
        mode: 0 = ganzes Bild, z = 100 % aufs Gesicht, c = Zuschneiden mit 10 × 15"""
        win.preset_row.set_selected(int(preset))
        state = {"n": 0}

        def done():
            state["n"] += 1
            if state["n"] > 1:
                return
            v = win.view
            if mode == "z":
                ax, ay = v.to_widget(v.iw * 0.52, v.ih * 0.18)
                v.zoom_100((ax, ay))
                GLib.timeout_add(3000, snap)
            elif mode == "c":
                win.start_crop()
                GLib.timeout_add(1500, lambda: (win.aspect_dd.set_selected(3), False)[1])
                GLib.timeout_add(2500, snap)
            else:
                GLib.timeout_add(600, snap)

        def snap():
            w, h = win.get_width(), win.get_height()
            pt = Gtk.WidgetPaintable.new(win)
            s = Gtk.Snapshot.new()
            pt.snapshot(s, w, h)
            node = s.to_node()
            tex = win.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(shot)
            print("Selbsttest:", win.info.get_text())
            self.quit()
            return False

        win.on_preview_done = done
        win.load_file(image)


def main():
    args = sys.argv[1:]
    app = App(args)
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
