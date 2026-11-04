#!/usr/bin/env bash
# Installiert den Plustek-ePhoto-Z300-Treiber (udev-Regel + SANE-Backend).
# Aufruf: sudo bash install.sh
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" = 0 ] || { echo "Bitte mit sudo ausführen: sudo bash install.sh"; exit 1; }
USER_NAME="${SUDO_USER:-root}"

echo "== Build-Abhängigkeiten"
apt-get install -y libusb-1.0-0-dev libsane-dev pkg-config

echo "== udev-Regel (Gerätezugriff ohne root)"
install -m644 tools/60-plustek-ephoto.rules /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --action=add --subsystem-match=usb --attr-match=idVendor=07b3

echo "== SANE-Backend bauen (als $USER_NAME) und installieren"
sudo -u "$USER_NAME" make -C sane clean
sudo -u "$USER_NAME" make -C sane
make -C sane install

echo "== Fertig. Test als normaler Benutzer:"
echo "   scanimage -L"
echo "   scanimage -d ephoto:ephoto:z300 --resolution 300 --format=png -o scan.png"
