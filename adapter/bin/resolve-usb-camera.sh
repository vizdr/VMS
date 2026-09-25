#!/usr/bin/env bash
# Discover the USB webcam's video node and ALSA capture card, instead of hardcoding them.
#
# Why this exists: the device paths were pinned to one physical unit --
#   CAM=/dev/v4l/by-id/usb-Generic_AVerMedia_PW310_Webcam_200901010001-video-index0
#   AUDIO_DEV=hw:CARD=Webcam,DEV=0
# The by-id path embeds the camera's USB **serial number**, so it breaks on a different
# unit of the *same model*, not just a different model. `by-path` is serial-free but
# encodes the physical USB port instead, so it breaks when you move the plug. Neither
# form survives a move to new hardware, which is what this resolves.
#
# Emits shell-sourceable lines, the same contract as camera-audio.py:
#   CAM_DEVICE=/dev/v4l/by-id/...
#   AUDIO_CARD=hw:CARD=...,DEV=0
# Absent values are simply not printed, so `eval` leaves any existing value alone.
#
# Precedence, highest first:
#   1. $CAM_DEVICE / $AUDIO_CARD already exported  -- an operator override always wins
#   2. the `cameras` registry (audioDevice)        -- applied by the caller, not here
#   3. discovery below
#
# Discovery is deliberately *loud* when ambiguous: with two USB cameras attached it picks
# the first and says so on stderr, because silently choosing one and being wrong is the
# failure mode that wastes an afternoon.

# --- video ---------------------------------------------------------------------------
# A UVC camera exposes two /dev/video* nodes; only one actually captures. Rather than
# guess by index, ask which one advertises a capture format -- on this hardware
# index0 lists 'MJPG' and index1 lists nothing at all.
if [ -n "${CAM_DEVICE:-}" ]; then
  echo "CAM_DEVICE=${CAM_DEVICE}"
else
  found=()
  for d in /dev/v4l/by-id/*-video-index*; do
    [ -e "$d" ] || continue
    if v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -qE "'(MJPG|YUYV|H264)'"; then
      found+=("$d")
    fi
  done
  # Fall back to raw /dev/video* if by-id is absent (no udev rules, unusual kernels).
  if [ ${#found[@]} -eq 0 ]; then
    for d in /dev/video*; do
      [ -e "$d" ] || continue
      if v4l2-ctl -d "$d" --list-formats 2>/dev/null | grep -qE "'(MJPG|YUYV|H264)'"; then
        found+=("$d")
      fi
    done
  fi
  if [ ${#found[@]} -gt 1 ]; then
    echo "resolve-usb-camera: ${#found[@]} capture devices found, using the first:" >&2
    printf '  %s\n' "${found[@]}" >&2
    echo "  set CAM_DEVICE explicitly to choose" >&2
  fi
  [ ${#found[@]} -gt 0 ] && echo "CAM_DEVICE=${found[0]}"
fi

# --- audio ---------------------------------------------------------------------------
# Match on "has a capture PCM and is not the Pi's onboard audio" rather than on the card
# name, which differs per device. `hw:CARD=<id>` is used rather than `hw:<N>` because
# card *numbers* are reassigned on reboot -- guide 18.2.
if [ -n "${AUDIO_CARD:-}" ]; then
  echo "AUDIO_CARD=${AUDIO_CARD}"
else
  for cdir in /proc/asound/card[0-9]*; do
    [ -d "$cdir" ] || continue
    compgen -G "$cdir/pcm*c" > /dev/null || continue     # no capture PCM on this card
    id=$(cat "$cdir/id" 2>/dev/null) || continue
    case "$id" in
      Headphones|ALSA|vc4*|HDMI*) continue ;;            # onboard Pi audio, never a mic
    esac
    echo "AUDIO_CARD=hw:CARD=${id},DEV=0"
    break
  done
fi
