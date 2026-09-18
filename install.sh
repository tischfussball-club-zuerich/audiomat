#!/usr/bin/env bash
# Install tfcz-audio for the current desktop user (do NOT run as root:
# PipeWire is a per-user session service, and so is this daemon).
#
#   ./install.sh            install/upgrade, enable the user service, start at boot
#   ./install.sh --no-boot  same, but do not enable start-at-boot (lingering)
#   ./uninstall.sh [--purge]   (or ./install.sh --uninstall)
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREFIX=${PREFIX:-$HOME/.local}
LIB=$PREFIX/share/tfcz-audio
BIN=$PREFIX/bin/tfcz-audio
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONFIG=${XDG_CONFIG_HOME:-$HOME/.config}/tfcz-audio/config.toml
ME=$(id -un)

if [[ $EUID -eq 0 ]]; then
  echo "run this as your normal desktop user, not root" >&2
  exit 1
fi

ENABLE_BOOT=1
[[ ${1:-} == "--no-boot" ]] && ENABLE_BOOT=0

if [[ ${1:-} == "--uninstall" ]]; then
  exec "$HERE/uninstall.sh"
fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1  ->  sudo apt install $2" >&2; exit 1; }; }
need python3 python3
need pw-loopback pipewire-bin
need pw-dump pipewire-bin
need wpctl wireplumber

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "python 3.11+ required" >&2; exit 1
fi

if [[ -z ${XDG_RUNTIME_DIR:-} ]]; then
  echo "XDG_RUNTIME_DIR is not set: this is not a desktop/user session." >&2
  echo "  -> run this from a terminal inside the logged-in desktop session," >&2
  echo "     or: export XDG_RUNTIME_DIR=/run/user/$(id -u) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u)/bus" >&2
  exit 1
fi
if ! systemctl --user show-environment >/dev/null 2>&1; then
  echo "cannot talk to your user systemd instance (systemctl --user)." >&2
  echo "  -> log in on the desktop and run this there; over ssh you need a running user session." >&2
  exit 1
fi
if command -v pactl >/dev/null 2>&1 && ! pactl info 2>/dev/null | grep -q "PipeWire"; then
  echo "The sound server is not PipeWire (PulseAudio?). Ubuntu 22.10+ uses PipeWire by default." >&2
  echo "  -> sudo apt install pipewire-audio wireplumber" >&2
  echo "     systemctl --user --now disable pulseaudio.service pulseaudio.socket" >&2
  echo "     systemctl --user --now enable pipewire pipewire-pulse wireplumber" >&2
  exit 1
fi
if ! systemctl --user is-active --quiet wireplumber; then
  echo "warning: wireplumber is not active; starting it (systemctl --user enable --now wireplumber)" >&2
  systemctl --user enable --now wireplumber || true
fi

# Pure stdlib package: copy it and write a wrapper. No pip, no network.
# A downloaded ZIP unpacks to a directory like "audiomat-main" and can never be
# updated with git pull, which makes it easy to keep installing an old snapshot.
if [[ ! -d $HERE/.git ]]; then
  echo
  echo "note: $HERE is not a git checkout, so 'git pull' cannot update it."
  echo "      For updates, clone the repository once and install from there:"
  echo "          git clone https://github.com/tischfussball-club-zuerich/audiomat.git"
  echo "          cd audiomat && ./install.sh"
  echo "      Afterwards:  git pull && ./install.sh"
  echo
fi

echo "==> installing package to $LIB"
rm -rf "$LIB/tfcz_audio"
mkdir -p "$LIB" "$PREFIX/bin"
cp -r "$HERE/tfcz_audio" "$LIB/tfcz_audio"
find "$LIB/tfcz_audio" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
# remember where the source lives, so the web UI can offer an update
printf '%s\n' "$HERE" > "$LIB/source-path"

cat > "$BIN" <<WRAP
#!/usr/bin/env bash
export PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m tfcz_audio "\$@"
WRAP
chmod 0755 "$BIN"
echo "==> installed $BIN ($("$BIN" --version))"

mkdir -p "$UNIT_DIR"
# the unit must point at the wrapper wherever PREFIX put it
sed "s|^ExecStart=.*|ExecStart=$BIN run|" "$HERE/systemd/tfcz-audio.service" > "$UNIT_DIR/tfcz-audio.service"
chmod 0644 "$UNIT_DIR/tfcz-audio.service"
systemctl --user daemon-reload
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "note: $PREFIX/bin is not in PATH of this shell yet; use $BIN or log out and in (Ubuntu adds ~/.local/bin at login)." ;;
esac

first_install=0
if [[ ! -f $CONFIG ]]; then
  first_install=1
  "$BIN" init-config "$CONFIG"   # starter config; the web UI wizard adds devices and connections
fi
systemctl --user enable tfcz-audio >/dev/null || true
# Type=notify: restart blocks until READY; never let a failed start abort the
# diagnostics below
systemctl --user restart tfcz-audio || true
sleep 2
if systemctl --user is-active --quiet tfcz-audio; then
  echo "==> service is running"
else
  echo "==> the service did NOT start. Last log lines:" >&2
  journalctl --user -u tfcz-audio -n 20 --no-pager >&2 || true
  echo "    fix the problem above, then: systemctl --user restart tfcz-audio" >&2
fi
# --- start at boot, without anyone logging in ------------------------------
# PipeWire and this daemon are per-user services. "Lingering" makes the user's
# service manager (and with it PipeWire, WirePlumber and tfcz-audio) start at
# boot instead of at login. Sound devices are then only reachable through the
# 'audio' group, because the usual per-login device permissions are missing.
if (( ENABLE_BOOT )); then
  echo
  echo "==> start at boot"
  if loginctl show-user "$ME" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
    echo "    lingering already enabled for $ME"
  elif loginctl enable-linger "$ME" 2>/dev/null; then
    echo "    lingering enabled: the audio router starts at boot, no login needed"
  elif command -v sudo >/dev/null 2>&1 && { echo "    (sudo password may be asked to enable start-at-boot)"; sudo loginctl enable-linger "$ME"; }; then
    echo "    lingering enabled (via sudo)"
  else
    echo "    could not enable lingering. Run:  sudo loginctl enable-linger $ME" >&2
  fi
  # 'audio': device access before the first login. 'pipewire': realtime
  # priority for the audio helpers (from /etc/security/limits.d/25-pw-rlimits.conf)
  # when no desktop session grants it via rtkit.
  missing_groups=()
  for g in audio pipewire; do
    getent group "$g" >/dev/null 2>&1 || continue
    id -nG "$ME" | tr ' ' '\n' | grep -qx "$g" || missing_groups+=("$g")
  done
  if (( ${#missing_groups[@]} == 0 )); then
    echo "    $ME is in the 'audio' and 'pipewire' groups (devices and realtime priority before login)"
  else
    echo "    adding $ME to the group(s): ${missing_groups[*]}  (device access / realtime priority before anyone logs in)"
    echo "    (sudo password may be asked)"
    if command -v sudo >/dev/null 2>&1 && sudo usermod -aG "$(IFS=,; echo "${missing_groups[*]}")" "$ME"; then
      echo "    done. Takes effect at the next boot (your running session keeps the old groups;"
      echo "    alternatively: sudo systemctl restart user@$(id -u)  -- this restarts all your session services)."
    else
      echo "    could not add the group(s). Run:  sudo usermod -aG $(IFS=,; echo "${missing_groups[*]}") $ME" >&2
    fi
  fi
fi

PORT=$(grep -E '^port *= *[0-9]+' "$CONFIG" | head -1 | grep -oE '[0-9]+' || echo 8787)

# Prove that the running service serves the files just installed. A plain
# `systemctl restart` re-runs the installed copy and cannot pick up a git pull.
installed_build=$(sha256sum "$LIB/tfcz_audio/ui.html" 2>/dev/null | cut -c1-8 || echo "?")
serving_build=$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -o '"build": *"[^"]*"' | cut -d'"' -f4 || true)
echo
if [[ -z $serving_build ]]; then
  echo "==> could not ask the running service for its version (is it up?)"
elif [[ $serving_build == "$installed_build" ]]; then
  echo "==> the service serves the files just installed (build $serving_build)"
else
  echo "==> WARNING: the service serves build ${serving_build:-unknown}, but ${installed_build} was installed" >&2
  echo "    something else is running: systemctl --user cat tfcz-audio | grep ExecStart" >&2
fi
echo
echo "==> checking the installation"
"$BIN" doctor || true
cat <<MSG

==> open the web UI:   http://127.0.0.1:${PORT}/
MSG
if (( first_install )); then
  cat <<MSG
    It opens the setup wizard: pick the headset of person A, person B and the
    game sound input (speak into a headset to see which one it is), then Connect.
    Nothing needs to be edited by hand.
MSG
fi
cat <<MSG
    useful:  tfcz-audio doctor | tfcz-audio fix | tfcz-audio status
             journalctl --user -u tfcz-audio -f
MSG
