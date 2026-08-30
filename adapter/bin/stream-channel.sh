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

CRED_ENDPOINT=c38gt2us7mrsmf.credentials.iot.eu-central-1.amazonaws.com
CERTS=/home/vladimir/MyProjects/VMS/certs

exec gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/${MEDIAMTX_PATH}" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! kvssink stream-name="${CAMERA_ID}" aws-region="eu-central-1" \
      iot-certificate="iot-certificate,endpoint=${CRED_ENDPOINT},cert-path=${CERTS}/adapter.cert.pem,key-path=${CERTS}/adapter.private.key,ca-path=${CERTS}/cacert.pem,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"
