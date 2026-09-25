#!/usr/bin/env bash
set -e
# $VMS_HOME if exported; otherwise the repo root this script lives in.
VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"
# Discovered, not hardcoded -- the old by-id path embedded this camera's USB serial.
eval "$(${VMS_HOME}/adapter/bin/resolve-usb-camera.sh)"
CAM="${CAM_DEVICE:?no capture-capable video device found}"

v4l2-ctl -d "$CAM" \
  --set-ctrl=auto_exposure=1 \
  --set-ctrl=exposure_dynamic_framerate=0 \
  --set-ctrl=white_balance_automatic=0 \
  --set-ctrl=focus_automatic_continuous=0

v4l2-ctl -d "$CAM" \
  --set-ctrl=exposure_time_absolute=250 \
  --set-ctrl=white_balance_temperature=4600 \
  --set-ctrl=focus_absolute=120
