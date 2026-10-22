# Shared by the KVS producer scripts (stream-cam01.sh, stream-cam02.sh, stream-channel.sh).
#
#   source "${VMS_HOME}/adapter/bin/producer-lib.sh"
#   producer_run stream-cam02 -v rtspsrc ... ! kvssink ...

# Lines that mean "the source is gone and this pipeline will never produce again".
# rtspsrc's message when MediaMTX closes a reader, and kvssink's acknowledgement of the
# resulting EOS. Matched as substrings against the pipeline's own output; see the comment
# on producer_run for why a watchdog is needed at all.
PRODUCER_FATAL_PATTERNS=(
  "The server closed the connection."
  "EOS Event received in sink"
  "Got EOS from element"
)

# The second watchdog: frames must keep reaching kvssink. The SDK logs "Kinesis Video client
# and stream metrics" on every key frame it *puts* into its buffer (KinesisVideoStream.cpp,
# statusPutFrame), before and independent of any upload, so the lines keep coming through an
# AWS outage, while kvssink buffers, and stop when the pipeline feeds it nothing. That is the
# failure the EOS patterns can't see: the source stays connected, the unit stays `active`, and
# nothing is uploaded. cam-02's audio branch couldn't take the camera's new G.711 law, and
# kvssink, which collects across its pads, then held the video too, for 7 minutes, with no
# metrics line at all (FoundAndFixed.md #70, #71). 45 s is ten of cam-02's 4 s keyframe
# intervals and covers the start (connect, first keyframe).
# The AAC encoder for a passthrough camera's G.711 audio (used unquoted, like VIDEO_CHAIN).
# `perfect-timestamp`: timestamps from the encoder's own sample count, the camera's RTP
# timestamps only checked for drift within `tolerance`. cam-02 sends its 40 ms packets in pairs
# that carry the *same* RTP timestamp (the rate is right, the stamps are not): with the default,
# voaacenc resynced on each backwards step and flagged ~1 AAC frame in 6 DISCONT, and kvssink
# drops every frame flagged only DISCONT, about 60 a minute, 128 ms of sound each
# (FoundAndFixed.md #72). 100 ms covers cam-02's 40 ms and keeps lip-sync within 0.1 s before a
# resync; measured 29 -> 1 DISCONT in 30 s on cam-02, 1 -> 1 on cam-04 (the stream's first).
AAC_ENC="voaacenc bitrate=32000 perfect-timestamp=true tolerance=100000000"

PRODUCER_FLOW_PATTERN="Kinesis Video client and stream metrics"
PRODUCER_STALL_SEC="${PRODUCER_STALL_SEC:-45}"

# Depayload/parse chain for a passthrough producer's video track, chosen by the codec
# MediaMTX is actually receiving (adapter/bin/stream-codec.py). cam-01 is H.264-only and
# keeps its own chain (measurements/codec-phase0.md §6).
#
# Both chains end in caps that make the parser emit codec_data, because kvssink sends codec
# private data only from the caps' codec_data field (gstkvssink.cpp). Its H.265 pad template
# pins no stream-format, so without `stream-format=hvc1` an Annex-B H.265 stream would
# ingest fine and never play back (MissingCodecPrivateData) -- the same ingest-vs-playback
# trap as FoundAndFixed.md #16. The h264 chain is token for token what every producer ran
# before H.265 existed.
#
# `h265timestamper` gives every frame a DTS. Some cameras' H.265 leaves h265parse with
# `dts: none`, and kvssink silently drops any buffer flagged HEADER without a valid DTS
# (gstkvssink.cpp, "Dropping frame with flag: 17920") -- that is every keyframe, so no
# fragment ever starts and PutMedia starves with 599s while the unit stays active
# (FoundAndFixed.md #57). The element computes DTS from PTS and the SPS reorder depth, so
# it is also right for a stream that has B-frames.
#
# `identity single-segment=true` after it: the timestamper shifts every timestamp by 1000 h
# (and its segment by the same, so running time is unchanged), and kvssink reads raw buffer
# timestamps. Alone that is harmless; next to an audio track, which stays near zero, KVS gets
# two clocks 1000 h apart and refuses audio frames in bursts (0x30000005, FoundAndFixed.md
# #58). Re-stamping to running time puts video on the audio's clock, as H.264 always was.
#
# H.264 deliberately gets neither. h264parse leaves passthrough H.264 at `dts: none` too, and
# h264timestamper is no cure: without a reorder count in the SPS it assumes the decoder's full
# buffer (4 frames at 1080p) and puts DTS 267 ms before PTS on cam-04, a stream with no
# B-frames, so KVS refused the *video* and no fragment completed. kvssink gives such a frame
# DTS = PTS instead (SDK patch 5, LAUNCH.md A3, FoundAndFixed.md #69); before that patch it
# invented `last_dts + 40 ms`, and beside audio KVS refused audio frames (0x30000005).
# A passthrough H.264 camera *with* B-frames would need h264timestamper here; none has any yet.
video_depay_chain() {
  case "$1" in
    h264) echo "rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=avc,alignment=au" ;;
    h265) echo "rtph265depay ! h265parse config-interval=-1 ! video/x-h265,stream-format=hvc1,alignment=au ! h265timestamper ! identity single-segment=true" ;;
    *)    echo "producer-lib: unsupported video codec '$1'" >&2; return 1 ;;
  esac
}

# Run the producer pipeline; never return success, and never sit there doing nothing.
#
# A producer must not end on its own. When MediaMTX closes the session -- the camera
# dropped off the network, the path's source changed, cam-01's publisher restarted --
# rtspsrc reports a *clean* end-of-stream. Under `exec` that exit status was the unit's,
# and Restart=on-failure treats 0 as a deliberate stop, so the cloud stream stayed down
# with the unit merely "inactive": indistinguishable from the user pressing Stop
# (FoundAndFixed.md #44).
#
# Turning that exit into a failure is necessary but, on this Pi, not sufficient: measured
# here, gst-launch does not exit at all. kvssink takes the EOS, its PutMedia connection
# then starves ("Operation too slow. Less than 30 bytes/sec"), the SDK logs "exited
# without triggering end-of-stream. Service call result: 599" and the process stays alive
# indefinitely -- observed 4+ minutes, uploading nothing, unit `active (running)`, which
# is worse than the original bug because the GUI reports it as streaming
# (FoundAndFixed.md #46). So the fatal lines are watched for directly, and the pipeline is
# killed when one appears.
#
# A real Stop never reaches the exit below: systemd signals the whole cgroup, which takes
# the reader loop and gst-launch with it (verified -- no orphan process survives a Stop).
producer_run() {
  local label="$1"; shift

  # gst-launch's output is read line by line rather than inherited, so it can be both
  # forwarded to the journal and matched against the patterns above.
  #
  # A named pipe, deliberately, after trying the two obvious alternatives:
  #   - `gst-launch ... | while read` runs the loop in a subshell, where $! is not the
  #     pipeline and cannot be killed;
  #   - `coproc` in its simple-command form silently sets neither GST nor GST_PID (bash
  #     only honours the name for a compound command), and in its block form $GST_PID is
  #     a wrapper subshell, so killing it would orphan the real gst-launch -- leaving a
  #     second producer on the same KVS stream after the restart.
  # With the FIFO, gst-launch stays a direct child, so `kill` and `wait` mean what they say.
  #
  # It lives in $RUNTIME_DIRECTORY, which the unit's `RuntimeDirectory=vms-producer` gives
  # us: systemd creates /run/vms-producer owned by the unit's User= and removes it when the
  # unit stops. That matters because a Stop kills the cgroup without unwinding this
  # function, so nothing here can clean up after itself -- and trapping TERM to try would
  # make the script exit on its own terms during a stop, which systemd records as a failed
  # unit. The fallbacks are for running a producer script by hand: the units run as an
  # unprivileged user, so /run itself is not writable to them. The name is fixed per camera
  # so that even in the fallback at most one stale FIFO exists per camera, reused by the
  # next start, rather than one accumulating per Stop.
  #
  # `stdbuf` keeps libc from holding output in a block buffer once stdout is a pipe, which
  # would delay detection until the buffer filled.
  local dir="${RUNTIME_DIRECTORY:-}"; dir="${dir%%:*}"
  if [ -z "$dir" ] || [ ! -d "$dir" ]; then
    dir="${XDG_RUNTIME_DIR:-}"
    [ -n "$dir" ] && [ -d "$dir" ] || dir="${TMPDIR:-/tmp}"
  fi
  local fifo="$dir/${label}.fifo"
  rm -f "$fifo"
  if ! mkfifo -m 600 "$fifo" 2>/dev/null; then
    # No pipe, no watchdog -- still better than `exec`, since any return is a failure.
    echo "${label}: could not create ${fifo}; running without the EOS watchdog" >&2
    gst-launch-1.0 "$@" || true
    echo "${label}: pipeline ended -- exiting non-zero so systemd restarts it" >&2
    exit 1
  fi

  stdbuf -oL -eL gst-launch-1.0 "$@" > "$fifo" 2>&1 &
  local pid=$!

  local line pat fatal= carry= flow=$SECONDS debug=
  while :; do
    # `read -t` so the stall check runs while the pipeline is silent too. A timeout can
    # return half a line: keep it and prepend it to the rest, so no line is split.
    if IFS= read -r -t 5 line; then
      line="${carry}${line}"; carry=
    elif [ $? -gt 128 ]; then
      carry="${carry}${line}"; line=
    else
      line="${carry}${line}"; carry=
      [ -n "$line" ] && printf '%s\n' "$line"
      break                                   # EOF: the pipeline is gone
    fi

    if [ -n "$line" ]; then
      printf '%s\n' "$line"
      case "$line" in
        *"$PRODUCER_FLOW_PATTERN"*) flow=$SECONDS ;;
      esac
      # The flow lines are the SDK's DEBUG output. Without it (a changed log level) the
      # stall check would kill healthy producers, so it only runs once DEBUG lines are seen.
      case "$line" in *"DEBUG -"*) debug=1 ;; esac
      for pat in "${PRODUCER_FATAL_PATTERNS[@]}"; do
        case "$line" in
          *"$pat"*)
            echo "${label}: '${pat}' -- the source closed the session; killing the pipeline" >&2
            # SIGKILL, not SIGTERM: the SDK is already wedged inside the teardown that a
            # clean shutdown would have to finish. The fragment in flight is lost either
            # way, and systemd brings the producer back on the live source 5 s later.
            kill -9 "$pid" 2>/dev/null
            fatal=1
            break
            ;;
        esac
      done
    fi

    if [ -z "$fatal" ] && [ -n "$debug" ] && [ $((SECONDS - flow)) -ge "$PRODUCER_STALL_SEC" ]; then
      echo "${label}: no frame reached kvssink for $((SECONDS - flow)) s -- killing the pipeline" >&2
      kill -9 "$pid" 2>/dev/null
      fatal=1
    fi
    # Stop reading now rather than waiting for the pipe to reach EOF. EOF needs *every*
    # writer to close, so anything the pipeline left behind still holding that descriptor
    # would keep this loop -- and therefore the restart -- blocked indefinitely, which is
    # the very failure being fixed.
    [ -n "$fatal" ] && break
  done < "$fifo"

  local rc=0
  wait "$pid" 2>/dev/null || rc=$?
  echo "${label}: pipeline ended (status ${rc}${fatal:+, killed by the watchdog}) -- exiting non-zero so systemd restarts it" >&2
  exit 1
}
