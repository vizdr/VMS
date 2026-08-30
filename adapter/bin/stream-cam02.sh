#!/usr/bin/env bash
set -e

CRED_ENDPOINT=c38gt2us7mrsmf.credentials.iot.eu-central-1.amazonaws.com
CERTS=/home/vladimir/MyProjects/VMS/certs

# Genuine passthrough -- no jpegdec/videoconvert/v4l2h264enc chain like stream-cam01.sh
# needs for the PW310. The camera already outputs real H.264 (§16.3(a)); this pipeline
# just depacketizes RTP and repackages for kvssink, which is why it's dramatically lighter
# on CPU (§16.3(b)'s point, made concrete rather than just argued).
exec gst-launch-1.0 -v \
  rtspsrc location="rtsp://127.0.0.1:8554/cam02" protocols=tcp latency=200 \
  ! rtph264depay ! h264parse config-interval=-1 \
  ! video/x-h264,stream-format=avc,alignment=au \
  ! kvssink stream-name="cam-02" aws-region="eu-central-1" \
      iot-certificate="iot-certificate,endpoint=${CRED_ENDPOINT},cert-path=${CERTS}/adapter.cert.pem,key-path=${CERTS}/adapter.private.key,ca-path=${CERTS}/cacert.pem,role-aliases=KVSAdapterRoleAlias,iot-thing-name=adapter-01"
