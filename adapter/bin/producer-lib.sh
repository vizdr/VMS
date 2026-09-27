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
video_depay_chain() {
  case "$1" in
    h264) echo "rtph264depay ! h264parse config-interval=-1 ! video/x-h264,stream-format=avc,alignment=au" ;;
    h265) echo "rtph265depay ! h265parse config-interval=-1 ! video/x-h265,stream-format=hvc1,alignment=au" ;;
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

  local line pat fatal=
  while IFS= read -r line; do
    printf '%s\n' "$line"
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
