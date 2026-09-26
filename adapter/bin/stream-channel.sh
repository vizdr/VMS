#!/usr/bin/env bash
# Generic passthrough launcher for any GUI-registered camera -- reads CAMERA_ID and
# MEDIAMTX_PATH from the environment (set per-instance by
# /etc/adapter/channels/<mediamtx-path>.env via kvs-cam@.service's EnvironmentFile=)
# instead of being duplicated per camera the way stream-cam01.sh/stream-cam02.sh were.
# Passthrough-only (§16.2.1/§16.3: WS-Discovery only ever finds networked ONVIF cameras,
# which this project already treats as pure passthrough, no transcode stage).
set -e
: "${CAMERA_ID:?CAMERA_ID must be set}"
: "${MEDIAMTX_PATH:?MEDIAMTX_PATH must be set}"
# $VMS_HOME if exported (interactive shells, via ~/.bashrc); otherwise the repo root
# this script lives in (systemd units do not read ~/.bashrc).
VMS_HOME="${VMS_HOME:-$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)}"

source "${VMS_HOME}/adapter/bin/adapter-config.sh"   # AWS_REGION, THING_NAME, IOT_* (/etc/adapter/adapter.env)
CERTS="${VMS_HOME}/certs"

exec gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/${MEDIAMTX_PATH}" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! kvssink stream-name="${CAMERA_ID}" aws-region="${AWS_REGION}" \
      iot-certificate="iot-certificate,endpoint=${IOT_CRED_ENDPOINT},cert-path=${CERTS}/adapter.cert.pem,key-path=${CERTS}/adapter.private.key,ca-path=${CERTS}/cacert.pem,role-aliases=${IOT_ROLE_ALIAS},iot-thing-name=${THING_NAME}"
