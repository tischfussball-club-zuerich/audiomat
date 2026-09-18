#!/usr/bin/env bash
# Remove tfcz-audio for the current user.
#
#   ./uninstall.sh          stop and remove service, wrapper and package; keep config and state
#   ./uninstall.sh --purge  also delete ~/.config/tfcz-audio and ~/.local/state/tfcz-audio
set -euo pipefail

PREFIX=${PREFIX:-$HOME/.local}
LIB=$PREFIX/share/tfcz-audio
BIN=$PREFIX/bin/tfcz-audio
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/tfcz-audio
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/tfcz-audio
ME=$(id -un)

if [[ $EUID -eq 0 ]]; then
  echo "run this as the user that installed tfcz-audio, not root" >&2
  exit 1
fi

purge=0
case ${1:-} in
  "") ;;
  --purge) purge=1 ;;
  *) echo "usage: $0 [--purge]" >&2; exit 2 ;;
esac

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now tfcz-audio 2>/dev/null && echo "==> service stopped and disabled" || true
fi
# make sure nothing lingers if the daemon was started by hand.
# Match on the executable name (comm) first so a shell whose command line
# merely contains these words is never killed.
kill_matching() {  # $1 = comm, $2 = regex on full args
  local pid
  for pid in $(pgrep -u "$ME" -x "$1" || true); do
    [[ $pid == "$$" || $pid == "$PPID" ]] && continue
    if ps -o args= -p "$pid" 2>/dev/null | grep -Eq -- "$2"; then
      kill "$pid" 2>/dev/null && echo "==> stopped $1 ($pid)" || true
    fi
  done
}
kill_matching python3 '(^|/)python3 -m tfcz_audio( |$)'
kill_matching tfcz-audio '.'
kill_matching pw-loopback '^pw-loopback -n tfcz\.'
kill_matching pw-record 'tfcz\.meter\.'

rm -f "$UNIT_DIR/tfcz-audio.service" && echo "==> removed systemd unit"
rm -f "$BIN" && echo "==> removed $BIN"
rm -rf "$LIB" && echo "==> removed $LIB"
command -v systemctl >/dev/null 2>&1 && systemctl --user daemon-reload || true

# drop-ins this tool wrote into PipeWire's config: they keep changing the buffer
# size and the sample rate of the whole machine long after the router is gone
PW_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
for dropin in "$PW_CONF"/10-tfcz-*.conf; do
  [[ -e $dropin ]] || continue
  rm -f "$dropin" && echo "==> removed PipeWire setting $(basename "$dropin")"
  restart_sound=1
done
if [[ ${restart_sound:-0} == 1 ]] && command -v systemctl >/dev/null 2>&1; then
  echo "    (the sound system keeps the old values until: systemctl --user restart pipewire wireplumber)"
fi

if (( purge )); then
  rm -rf "$CONFIG_DIR" "$STATE_DIR"
  echo "==> removed $CONFIG_DIR and $STATE_DIR"
else
  echo "kept config in $CONFIG_DIR and state in $STATE_DIR (use --purge to delete them)"
fi
if loginctl show-user "$ME" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
  echo "note: start-at-boot (lingering) for $ME is left enabled; to undo:  loginctl disable-linger $ME"
fi
echo "done. The OBS source 'TFCZ OBS Mic' disappears with the service; remove it from your OBS scene."
