#!/usr/bin/env bash
# Richtet ePhoto Scan für den aktuellen Benutzer ein: Python-Umgebung mit
# numpy/OpenCV (GTK kommt aus dem System) und Eintrag im GNOME-Menü.
# Aufruf ohne sudo:  bash app/install_app.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
APP="$PWD"

if [ ! -x .venv/bin/python ]; then
  echo "== Python-Umgebung"
  if command -v uv >/dev/null; then
    uv venv -q --system-site-packages -p /usr/bin/python3 .venv
    uv pip install -q -p .venv/bin/python numpy opencv-python-headless pyusb
  else
    /usr/bin/python3 -m venv --system-site-packages .venv
    .venv/bin/pip install -q numpy opencv-python-headless pyusb
  fi
fi
.venv/bin/python -c "import gi, cv2, numpy, usb; gi.require_version('Adw', '1')" \
  || { echo "GTK/libadwaita fehlen: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"; exit 1; }

echo "== Menüeintrag"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/io.github.bosstschopp_star.EphotoScan.desktop <<EOF
[Desktop Entry]
Type=Application
Name=ePhoto Scan
GenericName=Fotoscanner
Comment=Fotos mit dem Plustek ePhoto Z300 scannen und nachbearbeiten
Exec="$APP/.venv/bin/python" "$APP/ephoto_scan.py" %f
Icon=scanner
Terminal=false
Categories=Graphics;Scanning;Photography;GTK;
MimeType=image/png;image/jpeg;image/tiff;
StartupWMClass=io.github.bosstschopp_star.EphotoScan
EOF
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Fertig — „ePhoto Scan“ im Anwendungsmenü."
