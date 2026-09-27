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
source "${VMS_HOME}/adapter/bin/producer-lib.sh"     # producer_run
CERTS="${VMS_HOME}/certs"

# Video-only, but the audio pad is still linked -- to a fakesink. A GUI-registered ONVIF
# camera very often has a G.711 track that MediaMTX re-serves, and an rtspsrc pad nobody
# links can abort the whole pipeline with "streaming stopped, reason not-linked (-1)"
# depending on which pad is exposed first (FoundAndFixed.md #43). This matters more here
# than for cam-01/cam-02, because the camera behind this unit is unknown at write time.
# The branch is inert if the source has no audio track.
# Resolved from what MediaMTX actually receives, not assumed: a GUI-registered camera is
# whatever the operator plugged in, and H.265 is common on exactly these cameras. See
# stream-cam02.sh for why a missing source is a deliberate failure here.
VIDEO_ENV="$(${VMS_HOME}/venv-adapter/bin/python3 \
             ${VMS_HOME}/adapter/bin/stream-codec.py "${CAMERA_ID}")" \
  || { echo "stream-channel(${CAMERA_ID}): no H.264/H.265 video from MediaMTX -- exiting for systemd to retry" >&2; exit 1; }
eval "${VIDEO_ENV}"
VIDEO_CHAIN="$(video_depay_chain "${VIDEO_CODEC}")"
echo "stream-channel(${CAMERA_ID}): video ${VIDEO_CODEC}"

producer_run stream-channel -v \
  rtspsrc location="rtsp://127.0.0.1:8554/${MEDIAMTX_PATH}" protocols=tcp latency=200 name=src \
  src. ! application/x-rtp,media=video ! queue \
  ! ${VIDEO_CHAIN} \
  ! kvssink stream-name="${CAMERA_ID}" aws-region="${AWS_REGION}" \
      iot-certificate="iot-certificate,endpoint=${IOT_CRED_ENDPOINT},cert-path=${CERTS}/adapter.cert.pem,key-path=${CERTS}/adapter.private.key,ca-path=${CERTS}/cacert.pem,role-aliases=${IOT_ROLE_ALIAS},iot-thing-name=${THING_NAME}" \
  src. ! application/x-rtp,media=audio ! queue ! fakesink sync=false async=false
