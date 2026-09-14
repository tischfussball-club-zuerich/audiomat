#!/usr/bin/env bash
# Install tfcz-audio for the current desktop user (do NOT run as root:
# PipeWire is a per-user session service, and so is this daemon).
#
#   ./install.sh            install/upgrade, enable the user service
#   ./install.sh --uninstall
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREFIX=${PREFIX:-$HOME/.local}
LIB=$PREFIX/share/tfcz-audio
BIN=$PREFIX/bin/tfcz-audio
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/tfcz-audio/config.toml

if [[ $EUID -eq 0 ]]; then
  echo "run this as your normal desktop user, not root" >&2
  exit 1
fi

if [[ ${1:-} == "--uninstall" ]]; then
  systemctl --user disable --now tfcz-audio 2>/dev/null || true
  rm -f "$UNIT_DIR/tfcz-audio.service" "$BIN"
  rm -rf "$LIB"
  systemctl --user daemon-reload
  echo "removed. Config kept at $CONFIG"
  exit 0
fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1  ->  sudo apt install $2" >&2; exit 1; }; }
need python3 python3
need pw-loopback pipewire-bin
need pw-dump pipewire-bin
need wpctl wireplumber

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "python 3.11+ required" >&2; exit 1
fi

if ! systemctl --user is-active --quiet wireplumber; then
  echo "warning: wireplumber user service is not active; PipeWire with WirePlumber is required" >&2
fi

# Pure stdlib package: copy it and write a wrapper. No pip, no network.
echo "==> installing package to $LIB"
rm -rf "$LIB/tfcz_audio"
mkdir -p "$LIB" "$PREFIX/bin"
cp -r "$HERE/tfcz_audio" "$LIB/tfcz_audio"
find "$LIB/tfcz_audio" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
cat > "$BIN" <<WRAP
#!/usr/bin/env bash
export PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m tfcz_audio "\$@"
WRAP
chmod 0755 "$BIN"
echo "==> installed $BIN ($("$BIN" --version))"

mkdir -p "$UNIT_DIR"
install -m 0644 "$HERE/systemd/tfcz-audio.service" "$UNIT_DIR/tfcz-audio.service"
systemctl --user daemon-reload

if [[ -f $CONFIG ]]; then
  systemctl --user enable --now tfcz-audio
  systemctl --user restart tfcz-audio
  echo "==> service restarted with existing config $CONFIG"
  echo "    tfcz-audio status"
else
  "$BIN" init-config "$CONFIG"
  systemctl --user enable tfcz-audio
  cat <<MSG

==> next steps
  1. list your devices:            tfcz-audio devices
  2. fill in [devices] in:         $CONFIG
  3. validate:                     tfcz-audio check
  4. start:                        systemctl --user start tfcz-audio
  5. inspect:                      tfcz-audio status   |   journalctl --user -u tfcz-audio -f
MSG
fi
