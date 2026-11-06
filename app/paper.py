"""
Papiersensor des ePhoto Z300 abfragen — für den automatischen Scanstart.

Register 0x101, Bit 0x08 (DOCSENSOR): gesetzt = kein Papier.  Gelesen wird mit
einem einzelnen Vendor-Control-Transfer (dasselbe Protokoll wie sane/ephoto.c);
das Interface wird dabei nicht beansprucht, scanimage kann das Gerät also
jederzeit öffnen.  Während eines Scans ruht die Abfrage trotzdem, damit sie
nicht zwischen die Transfers des Backends gerät.
"""
import threading
import time

from i18n import _

VID, PID = 0x07B3, 0x143D
REG_STATUS, DOCSENSOR = 0x101, 0x08


class PaperWatcher:
    """Meldet über `on_paper()`, sobald ein Blatt `settle` Sekunden stabil eingelegt ist.

    Danach ruht er, bis das Blatt wieder heraus ist — ein Foto löst nur einen
    Scan aus.  `on_state(text)` beschreibt den Zustand für die Oberfläche.
    """

    def __init__(self, on_paper, on_state, interval=0.15, settle=0.8):
        self.on_paper, self.on_state = on_paper, on_state
        self.interval, self.settle = interval, settle
        self._armed = False
        self._paused = False
        self._wait_for_removal = False
        self._dev = None
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    # ---- Steuerung (aus dem GTK-Thread) ------------------------------------
    def set_armed(self, on):
        with self._lock:
            self._armed = on
            # liegt beim Einschalten schon ein Blatt im Einzug, zählt es als neu
            self._wait_for_removal = False

    def pause(self):
        """Vor dem Scan: Gerät freigeben und nicht mehr abfragen."""
        with self._lock:
            self._paused = True
            self._close()

    def resume(self, wait_for_removal=True):
        """Nach dem Scan: erst wieder auslösen, wenn das Blatt heraus ist."""
        with self._lock:
            self._paused = False
            self._wait_for_removal = wait_for_removal

    # ---- Abfrage ----------------------------------------------------------------
    def _open(self):
        import usb.core
        self._dev = usb.core.find(idVendor=VID, idProduct=PID)
        return self._dev is not None

    def _close(self):
        if self._dev is not None:
            try:
                import usb.util
                usb.util.dispose_resources(self._dev)
            except Exception:  # noqa: BLE001
                pass
        self._dev = None

    def _paper_present(self):
        r = REG_STATUS
        b = bytes(self._dev.ctrl_transfer(0xC0, 0x04, 0x8E | (r & 0x100), ((r & 0xFF) << 8) | 0x22, 2, 500))
        if len(b) != 2 or b[1] != 0x55:
            raise IOError(f"unerwartete Antwort {b.hex()}")
        return not (b[0] & DOCSENSOR)

    def _run(self):
        since = None
        last_state = None

        def state(text):
            nonlocal last_state
            if text != last_state:
                last_state = text
                self.on_state(text)

        while True:
            time.sleep(self.interval)
            with self._lock:
                armed, paused = self._armed, self._paused
                if not armed or paused:
                    since = None
                    if not armed:
                        self._close()
                        state(None)
                    continue
                try:
                    if self._dev is None and not self._open():
                        state(_("Scanner not found"))
                        time.sleep(1.5)
                        continue
                    present = self._paper_present()
                except Exception:  # noqa: BLE001  (abgezogen, belegt, Rechte)
                    self._close()
                    state(_("Scanner not reachable"))
                    time.sleep(1.5)
                    continue
                if self._wait_for_removal:
                    if present:
                        state(_("Remove the photo …"))
                        continue
                    self._wait_for_removal = False
                if not present:
                    since = None
                    state(_("Waiting for photo …"))
                    continue
                if since is None:
                    since = time.monotonic()
                    state(_("Photo detected …"))
                    continue
                if time.monotonic() - since < self.settle:
                    continue
                # auslösen: bis zum resume() ruhen
                since = None
                self._paused = True
                self._close()
            state(_("Scanning …"))
            self.on_paper()
