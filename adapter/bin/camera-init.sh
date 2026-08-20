#!/usr/bin/env bash
set -e
CAM=/dev/v4l/by-id/usb-Generic_AVerMedia_PW310_Webcam_200901010001-video-index0

v4l2-ctl -d "$CAM" \
  --set-ctrl=auto_exposure=1 \
  --set-ctrl=exposure_dynamic_framerate=0 \
  --set-ctrl=white_balance_automatic=0 \
  --set-ctrl=focus_automatic_continuous=0

v4l2-ctl -d "$CAM" \
  --set-ctrl=exposure_time_absolute=250 \
  --set-ctrl=white_balance_temperature=4600 \
  --set-ctrl=focus_absolute=120
