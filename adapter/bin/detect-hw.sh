#!/usr/bin/env bash
# Local hardware detection -- finds the Pi's USB camera, the microphone that belongs to it,
# the outage-buffer stick and the isolated CPU cores at every start, instead of scripts
# carrying one Pi's device names. Two uses:
#
#   source "${VMS_HOME}/adapter/bin/detect-hw.sh"   # functions, for the camera scripts
#   adapter/bin/detect-hw.sh --print [cam01]        # report what would be used (LAUNCH.md A9)
#
# Rules, so detection can't quietly pick the wrong device: exactly one match is used;
# none is an error; several is an error that lists them and asks for CAM_MATCH -- never a
# guess. Per-camera overrides live in /etc/adapter/cameras/<mediamtx-path>.env (template:
# config/cameras/cam01.env.example); every choice is logged to stderr, i.e. the journal.

source "$(dirname "${BASH_SOURCE[0]}")/env-file.sh"

ADAPTER_CAMERAS_DIR="${ADAPTER_CAMERAS_DIR:-/etc/adapter/cameras}"
V4L_BYID_DIR="${V4L_BYID_DIR:-/dev/v4l/by-id}"   # overridable only so the ambiguity path can be tested

# [match] -> the persistent /dev/v4l/by-id path of the one MJPG-capable capture node.
# Filtering on MJPG also drops UVC metadata nodes (the PW310's -video-index1 lists no
# formats at all), so the right node is found without assuming "index0".
find_uvc_camera() {
  local match="${1:-}" dev
  local found=()
  for dev in "$V4L_BYID_DIR"/*-video-index*; do
    [ -e "$dev" ] || continue
    [ -n "$match" ] && [[ "$(basename "$dev")" != *"$match"* ]] && continue
    v4l2-ctl -d "$dev" --list-formats 2>/dev/null | grep -q "'MJPG'" || continue
    found+=("$dev")
  done
  case ${#found[@]} in
    1) echo "${found[0]}" ;;
    0) echo "detect-hw: no MJPG-capable USB camera${match:+ matching '$match'} under $V4L_BYID_DIR" >&2
       return 1 ;;
    *) echo "detect-hw: ${#found[@]} MJPG-capable cameras -- set CAM_MATCH to choose one:" >&2
       printf '  %s\n' "${found[@]}" >&2
       return 2 ;;                           # ambiguous: waiting would not help
  esac
}

# <video device> -> hw:CARD=<id>,DEV=0 of the sound card on the SAME USB device. Matching
# by sysfs parent, not by card name: ALSA calls many webcams' mics just "Webcam", and a
# second one becomes "Webcam_1" in plug-in order (guide 18.1: never hw:N either).
alsa_card_for_video() {
  local node usb card
  node="$(basename "$(readlink -f "$1")")"
  usb="$(dirname "$(readlink -f "/sys/class/video4linux/$node/device")")"
  [[ "$usb" == */usb* ]] || return 1       # only USB cameras carry their own microphone
  for card in /sys/class/sound/card*; do
    [ "$(dirname "$(readlink -f "$card/device")")" = "$usb" ] || continue
    echo "hw:CARD=$(cat "$card/id"),DEV=0"
    return 0
  done
  return 1
}

# Mountpoint of the outage-buffer stick, found by the filesystem label README §6 gives it.
buffer_mount() {
  findmnt -n -o TARGET -S LABEL=vms-buffer 2>/dev/null
}

# Cores reserved from the scheduler (isolcpus, guide §1.4); empty when none are.
isolated_cpus() {
  cat /sys/devices/system/cpu/isolated 2>/dev/null
}

# Video codecs this machine can ENCODE in hardware, space-separated ("h264"; empty if none).
# GStreamer's V4L2 plugin registers v4l2h264enc / v4l2h265enc only when a matching M2M
# encoder device exists, so the element's presence is the hardware probe -- no device
# nodes, no Pi model. A Pi 4 has H.264 only (its HEVC block decodes, never encodes); a Pi 5
# has neither (measurements/codec-phase0.md §1).
hw_encoders() {
  local c found=()
  for c in h264 h265; do
    gst-inspect-1.0 "v4l2${c}enc" >/dev/null 2>&1 && found+=("$c")
  done
  echo "${found[*]}"
}

# <mediamtx-path>: load that camera's overrides and set CAM to its video device --
# CAM_DEVICE if the file pins one, otherwise the one camera matching CAM_MATCH.
#
# Waits up to CAMERA_WAIT_SEC (default 30) for the camera to *appear*: at boot the user
# services start seconds before a USB camera's /dev/v4l/by-id link exists, and a oneshot
# that gives up on the first look never runs again (FoundAndFixed.md #42). "Several
# cameras" is not a timing problem and fails at once.
camera_setup() {
  load_env_file "$ADAPTER_CAMERAS_DIR/$1.env"
  local deadline=$(( SECONDS + ${CAMERA_WAIT_SEC:-30} )) rc waited=0
  while :; do
    if [ -n "${CAM_DEVICE:-}" ]; then
      CAM="$CAM_DEVICE"; [ -e "$CAM" ] && rc=0 || rc=1
    else
      CAM="$(find_uvc_camera "${CAM_MATCH:-}" 2>/dev/null)" && rc=0 || rc=$?
    fi
    [ "$rc" -ne 1 ] || [ "$SECONDS" -ge "$deadline" ] && break
    [ "$waited" -eq 1 ] || echo "detect-hw: $1: no camera yet -- waiting up to ${CAMERA_WAIT_SEC:-30} s for it to appear" >&2
    waited=1; sleep 1
  done
  if [ "$rc" -ne 0 ]; then                  # repeat the failing lookup, this time with its message
    if [ -n "${CAM_DEVICE:-}" ]; then echo "detect-hw: $1: CAM_DEVICE=$CAM does not exist" >&2
    else find_uvc_camera "${CAM_MATCH:-}" >/dev/null; fi
    return 1
  fi
  echo "detect-hw: $1 video device: $CAM" >&2
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  if [ "${1:-}" = "--encoders" ]; then     # machine-readable, for adapter/codec_caps.py
    hw_encoders
    exit 0
  fi
  [ "${1:-}" = "--print" ] || { echo "usage: $0 --print [mediamtx-path, default cam01] | --encoders" >&2; exit 2; }
  path="${2:-cam01}"
  CAMERA_WAIT_SEC=0                        # a report, not a service start: don't wait
  echo "config file   : $ADAPTER_CAMERAS_DIR/$path.env $([ -r "$ADAPTER_CAMERAS_DIR/$path.env" ] && echo '(present)' || echo '(absent -- defaults)')"
  if camera_setup "$path" 2>/dev/null; then
    echo "video device  : $CAM -> $(readlink -f "$CAM")"
    echo "audio device  : $(alsa_card_for_video "$CAM" || echo 'none (no microphone on this camera)')"
  else
    camera_setup "$path"                   # again, to show the reason
    echo "video device  : NOT FOUND"; rc=1
  fi
  buf="$(buffer_mount)"
  echo "buffer mount  : ${buf:-not mounted (no filesystem LABEL=vms-buffer mounted)}"
  cpus="$(isolated_cpus)"
  echo "isolated CPUs : ${cpus:-none (no isolcpus -- pinning protects nothing)}"
  enc="$(hw_encoders)"
  echo "HW encoders   : ${enc:-none (every codec would need software encoding)}"
  exit "${rc:-0}"
fi
