"""
Bildbetrachter mit stufenlosem Zoom und Zuschneide-Werkzeug (GTK 4).

Koordinaten: Bildkoordinaten sind Pixel des geometrisch bearbeiteten Bildes in
voller Auflösung.  `z` ist der Zoom in logischen Bildschirmpixeln je Bildpixel;
100 % heisst ein Gerätepixel je Bildpixel (auf HiDPI-Bildschirmen z = 1/Skalierung).

Angezeigt werden zwei Ebenen:
  - die Vorschau (ganzes Bild, verkleinert gerechnet) — sofort da
  - darüber der sichtbare Ausschnitt, in Bildschirmauflösung neu gerechnet,
    sobald die Vorschau beim Hineinzoomen nicht mehr genug Pixel hat.  Er wird
    über `detail_provider(rect, scale, token, callback)` angefordert.

Bedienung:
  Mausrad                       zoomen um den Mauszeiger
  Touchpad: Pinch / Strg+Wisch  zoomen;   Wischen  verschieben
  Ziehen                        verschieben (wenn hineingezoomt)
  Doppelklick                   Ganz <-> 100 %
"""
import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk  # noqa: E402

from i18n import _  # noqa: E402

MAX_ZOOM_DEV = 8.0        # höchstens 800 %
HANDLE = 10               # Fangradius der Anfasser in logischen Pixeln
MIN_CROP = 32             # kleinster Zuschnitt in Bildpixeln
CROP_BAR = 84             # unten freigehaltener Platz für die Werkzeugleiste beim Zuschneiden


def _rgba(r, g, b, a):
    c = Gdk.RGBA()
    c.red, c.green, c.blue, c.alpha = r, g, b, a
    return c


def _rect(x, y, w, h):
    return Graphene.Rect().init(x, y, max(w, 0), max(h, 0))


class ImageView(Gtk.Widget):
    __gtype_name__ = "EphotoImageView"
    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "crop-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, detail_provider):
        super().__init__(hexpand=True, vexpand=True, focusable=True)
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.provider = detail_provider
        self.base = None              # Textur der Vorschau
        self.base_scale = 1.0         # Texturpixel je Bildpixel
        self.iw = self.ih = 0         # Bildgrösse
        self.detail = None            # (Textur, (x, y, w, h))
        self.z = None                 # None = einpassen
        self.cx = self.cy = 0.0       # Bildpunkt in der Mitte der Ansicht
        self.pointer = None
        self._token = 0
        self._detail_timer = None
        self.crop_mode = False
        self.crop = None              # [x0, y0, x1, y1] in Bildpixeln
        self.aspect = None            # Breite/Höhe oder None = frei
        self._drag = None

        scroll = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", lambda *_: setattr(self, "pointer", None))
        self.add_controller(motion)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", lambda g, _s: setattr(self, "_pinch_z", self.zoom()))
        pinch.connect("scale-changed", self._on_pinch)
        self.add_controller(pinch)

    # ---- Bild ---------------------------------------------------------------
    def set_base(self, texture, size, base_scale):
        """Neue Vorschau.  Ändert sich die Bildgrösse (Drehen, Zuschnitt), wird eingepasst."""
        if size != (self.iw, self.ih):
            self.z = None
            self.cx, self.cy = size[0] / 2, size[1] / 2
        self.base, self.base_scale = texture, base_scale
        self.iw, self.ih = size
        self.detail = None
        self._token += 1
        self._schedule_detail()
        self.queue_draw()

    def clear(self):
        self.base = self.detail = None
        self.iw = self.ih = 0
        self.queue_draw()

    def deliver_detail(self, token, texture, rect):
        if token == self._token:
            self.detail = (texture, rect)
            self.queue_draw()

    # ---- Zoom ---------------------------------------------------------------
    def _view_size(self):
        """Nutzbare Fläche; beim Zuschneiden bleibt unten Platz für die Werkzeugleiste."""
        W, H = self.get_width(), self.get_height()
        return W, (H - CROP_BAR if self.crop_mode else H)

    def fit_zoom(self):
        W, H = self._view_size()
        if not self.iw or W < 2 or H < 2:
            return 1.0
        return min(W / self.iw, H / self.ih) * (0.94 if self.crop_mode else 0.98)

    def zoom(self):
        return self.z if self.z is not None else self.fit_zoom()

    def zoom_percent(self):
        return self.zoom() * self.get_scale_factor() * 100

    def is_fit(self):
        return self.z is None

    def set_zoom(self, z, anchor=None):
        """Zoom setzen; der Bildpunkt unter `anchor` (Widgetkoordinaten) bleibt stehen."""
        if not self.iw:
            return
        fit = self.fit_zoom()
        z = min(max(z, fit), MAX_ZOOM_DEV / self.get_scale_factor())
        W, H = self._view_size()
        if anchor is None:
            anchor = (W / 2, H / 2)
        ix, iy = self.to_image(*anchor)
        self.z = None if z <= fit * 1.001 else z
        if self.z is None:
            self.cx, self.cy = self.iw / 2, self.ih / 2
        else:
            self.cx = ix - (anchor[0] - W / 2) / z
            self.cy = iy - (anchor[1] - H / 2) / z
        self._changed()

    def zoom_by(self, factor, anchor=None):
        self.set_zoom(self.zoom() * factor, anchor or self.pointer)

    def zoom_fit(self):
        self.z = None
        self.cx, self.cy = self.iw / 2, self.ih / 2
        self._changed()

    def zoom_100(self, anchor=None):
        self.set_zoom(1.0 / self.get_scale_factor(), anchor)

    def _clamp(self):
        z = self.zoom()
        W, H = self._view_size()
        for attr, size, view in (("cx", self.iw, W), ("cy", self.ih, H)):
            half = view / (2 * z)
            v = getattr(self, attr)
            v = size / 2 if size * z <= view else min(max(v, half), size - half)
            setattr(self, attr, v)

    def _origin(self):
        if self.z is None:
            self.cx, self.cy = self.iw / 2, self.ih / 2
        self._clamp()
        z = self.zoom()
        W, H = self._view_size()
        return W / 2 - self.cx * z, H / 2 - self.cy * z, z

    def to_image(self, x, y):
        ox, oy, z = self._origin()
        return (x - ox) / z, (y - oy) / z

    def to_widget(self, ix, iy):
        ox, oy, z = self._origin()
        return ox + ix * z, oy + iy * z

    def _changed(self):
        self.detail = None if self.detail and not self._detail_covers() else self.detail
        self._schedule_detail()
        self.queue_draw()
        self.emit("view-changed")

    # ---- Ausschnitt in voller Qualität -----------------------------------------
    def _needs_detail(self):
        return self.base is not None and self.zoom() * self.get_scale_factor() > self.base_scale * 1.05

    def _visible_rect(self):
        ox, oy, z = self._origin()
        x0, y0 = max(0, -ox / z), max(0, -oy / z)
        x1, y1 = min(self.iw, (self.get_width() - ox) / z), min(self.ih, (self.get_height() - oy) / z)
        return x0, y0, x1, y1

    def _detail_covers(self):
        x0, y0, x1, y1 = self._visible_rect()
        _tex, (dx, dy, dw, dh), dscale = self.detail[0], self.detail[1][:4], self.detail[1][4]
        good_res = abs(dscale - min(1.0, self.zoom() * self.get_scale_factor())) < 0.02
        return good_res and dx <= x0 + 1 and dy <= y0 + 1 and dx + dw >= x1 - 1 and dy + dh >= y1 - 1

    def _schedule_detail(self):
        if self._detail_timer:
            GLib.source_remove(self._detail_timer)
        self._detail_timer = GLib.timeout_add(120, self._request_detail)

    def _request_detail(self):
        self._detail_timer = None
        if not self._needs_detail() or (self.detail and self._detail_covers()):
            return False
        x0, y0, x1, y1 = self._visible_rect()
        # etwas Rand, damit kleines Verschieben ohne Neuberechnung auskommt
        mx, my = (x1 - x0) * 0.15, (y1 - y0) * 0.15
        rect = (int(max(0, x0 - mx)), int(max(0, y0 - my)),
                int(min(self.iw, x1 + mx)), int(min(self.ih, y1 + my)))
        scale = min(1.0, self.zoom() * self.get_scale_factor())
        self._token += 1
        self.provider((rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]), scale, self._token)
        return False

    # ---- Zeichnen ----------------------------------------------------------------
    def do_snapshot(self, snap):
        if self.base is None or not self.iw:
            return
        ox, oy, z = self._origin()
        W, H = self.get_width(), self.get_height()
        snap.append_scaled_texture(self.base, Gsk.ScalingFilter.TRILINEAR,
                                   _rect(ox, oy, self.iw * z, self.ih * z))
        if self.detail and self._needs_detail():
            tex, (dx, dy, dw, dh, _s) = self.detail
            filt = Gsk.ScalingFilter.NEAREST if z * self.get_scale_factor() >= 2 else Gsk.ScalingFilter.LINEAR
            snap.append_scaled_texture(tex, filt, _rect(ox + dx * z, oy + dy * z, dw * z, dh * z))
        if self.crop_mode and self.crop:
            self._snapshot_crop(snap, W, H)

    def _snapshot_crop(self, snap, W, H):
        x0, y0 = self.to_widget(self.crop[0], self.crop[1])
        x1, y1 = self.to_widget(self.crop[2], self.crop[3])
        shade = _rgba(0, 0, 0, 0.6)
        for r in (_rect(0, 0, W, y0), _rect(0, y1, W, H - y1), _rect(0, y0, x0, y1 - y0),
                  _rect(x1, y0, W - x1, y1 - y0)):
            snap.append_color(shade, r)
        white, thin = _rgba(1, 1, 1, 0.95), _rgba(1, 1, 1, 0.45)
        w, h = x1 - x0, y1 - y0
        for i in (1, 2):                                       # Drittel-Linien
            snap.append_color(thin, _rect(x0 + w * i / 3, y0, 1, h))
            snap.append_color(thin, _rect(x0, y0 + h * i / 3, w, 1))
        for r in (_rect(x0, y0, w, 1.5), _rect(x0, y1 - 1.5, w, 1.5),
                  _rect(x0, y0, 1.5, h), _rect(x1 - 1.5, y0, 1.5, h)):
            snap.append_color(white, r)
        L, T = 18, 4                                           # Eck- und Kantenanfasser
        for (cx, cy, sx, sy) in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            snap.append_color(white, _rect(cx if sx > 0 else cx - L, cy - (0 if sy > 0 else T), L, T))
            snap.append_color(white, _rect(cx - (0 if sx > 0 else T), cy if sy > 0 else cy - L, T, L))
        for (cx, cy, horiz) in (((x0 + x1) / 2, y0, True), ((x0 + x1) / 2, y1, True),
                                (x0, (y0 + y1) / 2, False), (x1, (y0 + y1) / 2, False)):
            if horiz:
                snap.append_color(white, _rect(cx - L / 2, cy - T / 2, L, T))
            else:
                snap.append_color(white, _rect(cx - T / 2, cy - L / 2, T, L))

    # ---- Eingaben -------------------------------------------------------------------
    def _on_motion(self, _c, x, y):
        self.pointer = (x, y)
        if self._drag is None:
            self.set_cursor_from_name(self._cursor_for(self._hit(x, y)))

    def _on_scroll(self, ctrl, dx, dy):
        if not self.iw:
            return False
        state = ctrl.get_current_event_state()
        wheel = ctrl.get_unit() == Gdk.ScrollUnit.WHEEL
        if wheel or state & Gdk.ModifierType.CONTROL_MASK:
            step = dy if wheel else dy / 10
            self.zoom_by(1.25 ** (-step), self.pointer)
        else:                                                  # Touchpad: verschieben
            z = self.zoom()
            self.cx += dx / z
            self.cy += dy / z
            self._changed()
        return True

    def _on_pinch(self, _g, scale):
        _ok, x, y = _g.get_bounding_box_center()
        self.set_zoom(self._pinch_z * scale, (x, y))

    def _on_pressed(self, gesture, n, x, y):
        self.grab_focus()
        if n == 2 and not self.crop_mode:
            if self.is_fit() or abs(self.zoom_percent() - 100) > 1:
                self.zoom_100((x, y))
            else:
                self.zoom_fit()

    def _hit(self, x, y):
        if not self.crop_mode or not self.crop:
            return "pan" if not self.is_fit() else None
        x0, y0 = self.to_widget(self.crop[0], self.crop[1])
        x1, y1 = self.to_widget(self.crop[2], self.crop[3])
        near = lambda a, b: abs(a - b) <= HANDLE   # noqa: E731
        inside_y, inside_x = y0 - HANDLE <= y <= y1 + HANDLE, x0 - HANDLE <= x <= x1 + HANDLE
        h = ("w" if near(x, x0) else "e" if near(x, x1) else "") if inside_y else ""
        v = ("n" if near(y, y0) else "s" if near(y, y1) else "") if inside_x else ""
        if v + h:
            return v + h
        if x0 < x < x1 and y0 < y < y1:
            return "move"
        return "new"

    @staticmethod
    def _cursor_for(hit):
        return {"n": "n-resize", "s": "s-resize", "e": "e-resize", "w": "w-resize",
                "ne": "ne-resize", "nw": "nw-resize", "se": "se-resize", "sw": "sw-resize",
                "move": "move", "new": "crosshair", "pan": "grab"}.get(hit, "default")

    def _on_drag_begin(self, _g, x, y):
        hit = self._hit(x, y)
        if hit is None:
            return
        self._drag = (hit, x, y, self.cx, self.cy, list(self.crop) if self.crop else None)
        if hit == "pan":
            self.set_cursor_from_name("grabbing")

    def _on_drag_update(self, _g, dx, dy):
        if not self._drag:
            return
        hit, sx, sy, cx, cy, crop0 = self._drag
        z = self.zoom()
        if hit == "pan":
            self.cx, self.cy = cx - dx / z, cy - dy / z
            self._changed()
            return
        ix, iy = self.to_image(sx + dx, sy + dy)
        if hit == "move":
            w, h = crop0[2] - crop0[0], crop0[3] - crop0[1]
            x0 = min(max(crop0[0] + dx / z, 0), self.iw - w)
            y0 = min(max(crop0[1] + dy / z, 0), self.ih - h)
            self.crop = [x0, y0, x0 + w, y0 + h]
        elif hit == "new":
            ax, ay = self.to_image(sx, sy)
            self.crop = self._fit_aspect([min(ax, ix), min(ay, iy), max(ax, ix), max(ay, iy)],
                                         anchor=(ax, ay))
        else:
            self.crop = self._resize(crop0, hit, ix, iy)
        self.queue_draw()
        self.emit("crop-changed")

    def _on_drag_end(self, *_):
        self._drag = None
        if self.pointer:
            self.set_cursor_from_name(self._cursor_for(self._hit(*self.pointer)))

    # ---- Zuschneiden ----------------------------------------------------------------
    def begin_crop(self, rect=None):
        self.crop_mode = True
        self.crop = list(rect) if rect else [0, 0, self.iw, self.ih]
        self.zoom_fit()

    def end_crop(self):
        self.crop_mode = False
        self.queue_draw()

    def crop_relative(self):
        x0, y0, x1, y1 = self.crop
        return (x0 / self.iw, y0 / self.ih, x1 / self.iw, y1 / self.ih)

    def set_aspect(self, ratio):
        """Seitenverhältnis Breite/Höhe (None = frei); der Rahmen wird um seine Mitte angepasst."""
        self.aspect = ratio
        if ratio and self.crop:
            x0, y0, x1, y1 = self.crop
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            w, h = x1 - x0, y1 - y0
            if w / h > ratio:
                w = h * ratio
            else:
                h = w / ratio
            self.crop = self._clamp_rect([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])
            self.queue_draw()
            self.emit("crop-changed")

    def _clamp_rect(self, r):
        x0, y0, x1, y1 = r
        w, h = min(x1 - x0, self.iw), min(y1 - y0, self.ih)
        if self.aspect:                                # beim Verkleinern Verhältnis halten
            if w / max(h, 1) > self.aspect:
                w = h * self.aspect
            else:
                h = w / self.aspect
        x0 = min(max(x0, 0), self.iw - w)
        y0 = min(max(y0, 0), self.ih - h)
        return [x0, y0, x0 + w, y0 + h]

    def _fit_aspect(self, r, anchor):
        x0, y0, x1, y1 = r
        if self.aspect:
            w, h = x1 - x0, y1 - y0
            if w / max(h, 1e-6) > self.aspect:
                h = w / self.aspect
            else:
                w = h * self.aspect
            ax, ay = anchor
            x0, x1 = (ax, ax + w) if x0 >= ax else (ax - w, ax)
            y0, y1 = (ay, ay + h) if y0 >= ay else (ay - h, ay)
        r = [max(0, x0), max(0, y0), min(self.iw, x1), min(self.ih, y1)]
        if r[2] - r[0] < MIN_CROP or r[3] - r[1] < MIN_CROP:
            return self.crop
        return r

    def _resize(self, r, hit, ix, iy):
        x0, y0, x1, y1 = r
        ix, iy = min(max(ix, 0), self.iw), min(max(iy, 0), self.ih)
        if "w" in hit:
            x0 = min(ix, x1 - MIN_CROP)
        if "e" in hit:
            x1 = max(ix, x0 + MIN_CROP)
        if "n" in hit:
            y0 = min(iy, y1 - MIN_CROP)
        if "s" in hit:
            y1 = max(iy, y0 + MIN_CROP)
        if self.aspect:
            w, h = x1 - x0, y1 - y0
            if hit in ("n", "s"):                  # Kante: Breite symmetrisch nachziehen
                w = h * self.aspect
                cx = (r[0] + r[2]) / 2
                x0, x1 = cx - w / 2, cx + w / 2
            elif hit in ("e", "w"):
                h = w / self.aspect
                cy = (r[1] + r[3]) / 2
                y0, y1 = cy - h / 2, cy + h / 2
            else:                                  # Ecke: gegenüberliegende Ecke bleibt fest
                if w / h > self.aspect:
                    h = w / self.aspect
                else:
                    w = h * self.aspect
                if "w" in hit:
                    x0 = x1 - w
                else:
                    x1 = x0 + w
                if "n" in hit:
                    y0 = y1 - h
                else:
                    y1 = y0 + h
            if x0 < 0 or y0 < 0 or x1 > self.iw or y1 > self.ih:
                return r                           # passt nicht mehr ins Bild: stehen bleiben
        return [x0, y0, x1, y1]


def zoom_label(view):
    if not view.iw:
        return "–"
    return _("Fit") if view.is_fit() else f"{math.floor(view.zoom_percent() + 0.5)} %"
