#!/usr/bin/env bash
# Install legion-lights on Omarchy / Arch. Run from this folder: ./install.sh
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Installing legion-lights to /usr/local/bin"
sudo install -Dm755 legion_lights.py /usr/local/bin/legion-lights
sudo install -Dm755 legion-lights-menu /usr/local/bin/legion-lights-menu

echo "==> Installing udev rule (lets you use the controller without sudo)"
sudo install -Dm644 70-legion-lights.rules /etc/udev/rules.d/70-legion-lights.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=usb --attr-match=idVendor=17ef
sudo udevadm trigger --subsystem-match=hidraw

echo "==> Installing suspend/resume hook"
tmp=$(mktemp)
sed "s/@USER@/$USER/" systemd/legion-lights-sleep > "$tmp"
sudo install -Dm755 "$tmp" /usr/lib/systemd/system-sleep/legion-lights
rm -f "$tmp"

hooks="$HOME/.config/omarchy/hooks"
echo "==> Installing Omarchy hooks (theme-set: follow theme; post-boot: restore lights)"
install -Dm755 hooks/theme-set "$hooks/theme-set.d/legion-lights"
install -Dm755 hooks/post-boot "$hooks/post-boot.d/legion-lights"

echo
legion-lights list || true
echo
echo "Done. Try:  legion-lights info   then   legion-lights color purple"
echo "Menu: add to ~/.config/hypr/bindings.lua a binding that runs: legion-lights-menu"
