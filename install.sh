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
sudo udevadm trigger --subsystem-match=hidraw

echo "==> Installing suspend/resume hook"
sed "s/@USER@/$USER/" systemd/legion-lights-sleep > /tmp/legion-lights-sleep
sudo install -Dm755 /tmp/legion-lights-sleep /usr/lib/systemd/system-sleep/legion-lights
rm /tmp/legion-lights-sleep

echo "==> Installing Omarchy theme hook"
hook="$HOME/.config/omarchy/hooks/theme-set"
mkdir -p "$(dirname "$hook")"
if [[ -f $hook ]] && ! grep -q legion-lights "$hook"; then
  echo 'legion-lights theme --if-following' >> "$hook"
elif [[ ! -f $hook ]]; then
  install -m755 hooks/theme-set "$hook"
fi

echo "==> Restoring lights at login"
autostart="$HOME/.config/hypr/autostart.conf"
if [[ -f $autostart ]] && ! grep -q legion-lights "$autostart"; then
  echo 'exec-once = legion-lights restore' >> "$autostart"
fi

echo
legion-lights list || true
echo
echo "Done. Try:  legion-lights info   then   legion-lights color purple"
echo "Menu: add to ~/.config/hypr/bindings.conf:"
echo "  bindd = SUPER ALT, L, Case lighting, exec, legion-lights-menu"
