#!/usr/bin/env bash
# Lock cam-01's exposure / white balance / focus before streaming starts (guide §2.3).
# The camera is found by detection and the control values come from
# /etc/adapter/cameras/cam01.env when present -- defaults below are the PW310 tuning.
set -e
VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"
source "${VMS_HOME}/adapter/bin/detect-hw.sh"
camera_setup cam01                      # sets CAM, or exits: no camera, or ambiguous

: "${V4L2_MODE_CTRLS:=auto_exposure=1,exposure_dynamic_framerate=0,white_balance_automatic=0,focus_automatic_continuous=0}"
: "${V4L2_VALUE_CTRLS:=exposure_time_absolute=250,white_balance_temperature=4600,focus_absolute=120}"

if [ "${1:-}" = "--print-config" ]; then
  printf 'V4L2_MODE_CTRLS=%s\nV4L2_VALUE_CTRLS=%s\n' "$V4L2_MODE_CTRLS" "$V4L2_VALUE_CTRLS"
  exit 0
fi

# Controls are applied only if THIS camera has them. Every webcam exposes a different set
# -- fixed-focus models have no focus_absolute at all -- and `v4l2-ctl --set-ctrl` exits 1
# on an unknown control, which under `set -e` aborted the whole script at the first one:
# every control after it was silently never applied, and the unit then failed and retried
# for ever. Skipping what the device lacks is what makes a camera swap survivable, and it
# is reported rather than hidden, because "this camera cannot do that" is worth knowing.
supported="$(v4l2-ctl -d "$CAM" --list-ctrls 2>/dev/null | sed -n 's/^[[:space:]]*\([a-zA-Z0-9_]\{1,\}\) 0x[0-9a-f]\{8\}.*/\1/p')"
rejected=0

apply_ctrls() {                       # $1 = label, rest = name=value pairs
  local label="$1"; shift
  local wanted=() skipped=() c
  for c in "$@"; do
    [ -n "$c" ] || continue
    if printf '%s\n' "$supported" | grep -qx -- "${c%%=*}"; then wanted+=("$c"); else skipped+=("${c%%=*}"); fi
  done
  [ ${#skipped[@]} -eq 0 ] || echo "camera-init: $label: this camera has no ${skipped[*]} -- skipped" >&2
  [ ${#wanted[@]} -gt 0 ] || return 0
  # One call for the whole pass; if the driver refuses it, retry singly so that one bad
  # value cannot discard the rest, and name the one that failed.
  # Note: the driver CLAMPS an out-of-range value rather than refusing it (a requested
  # white_balance_temperature=99999 came back as the maximum, 6500), so this counts real
  # refusals only. Anything that needs the value to be exact must read it back.
  if ! v4l2-ctl -d "$CAM" "${wanted[@]/#/--set-ctrl=}" 2>/dev/null; then
    for c in "${wanted[@]}"; do
      v4l2-ctl -d "$CAM" --set-ctrl="$c" 2>/dev/null || { echo "camera-init: $label: rejected $c" >&2; rejected=$((rejected+1)); }
    done
  fi
}

# Two passes, modes first: a value like exposure_time_absolute is rejected while its
# automatic mode is still on.
IFS=, read -ra MODES  <<< "$V4L2_MODE_CTRLS"
IFS=, read -ra VALUES <<< "$V4L2_VALUE_CTRLS"
apply_ctrls modes  "${MODES[@]}"
apply_ctrls values "${VALUES[@]}"

# A rejected value is a configuration error, not a transient one, so it does NOT fail the
# unit: Restart=on-failure (FoundAndFixed.md #42) would then retry it for ever. A camera
# that is absent or ambiguous still fails, in camera_setup above, because that one is
# worth retrying.
[ "$rejected" -eq 0 ] || echo "camera-init: $rejected control(s) rejected -- the camera is running with the rest" >&2
exit 0
