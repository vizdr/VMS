#!/usr/bin/env python3
"""Phase 2 (Camera-Features.md §9): turn ONVIF detections into evidence clips.

Subscribes to each camera's ONVIF event stream and, on a *rising* detection edge,
publishes one message to `adapter/adapter-01/event`. Everything downstream already
exists and is untouched:

    adapter/+/event  ->  IoT Rule  ->  clip-to-s3 Lambda  ->  S3 + clips table  ->  GUIs

`clip_to_s3.py` cuts ts-12s .. ts+33s, so a detection costs exactly one 45-second clip.

Three design choices worth stating, all of them load-bearing:

* **Separate process from agent.py.** If this event loop wedges or the camera drops,
  Start/Stop over MQTT must keep working. They share modules, not a process.
* **Publishes over the HTTP data plane (iot-data), not MQTT.** agent.py already holds the
  one MQTT session the IoT policy permits -- it allows `iot:Connect` only on
  `client/${iot:Connection.Thing.ThingName}`, i.e. `adapter-01`. A second MQTT connection
  would be refused, or worse would kick the agent off, since IoT Core drops the older
  session on a duplicate client ID. `iot-data:Publish` reaches the same topic and the
  same IoT Rule without contending for that identity.
* **Cooldown is mandatory, not a tuning nicety.** Phase 0 measured the detectors latching
  for ~5 s and re-arming repeatedly: one person crossing the hall produced dozens of
  rising edges. Without a minimum gap that becomes dozens of overlapping 45-second clips,
  each with its own GetClip and S3 PUT.

Usage:
  event_watcher.py                 # act for real
  event_watcher.py --dry-run       # log what it *would* publish, create nothing
"""
import os
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config

from aws_device_creds import get_session
from camera_control import is_unregistered

# $VMS_HOME if set, otherwise the repo root (this file lives in adapter/).
VMS_HOME = os.environ.get("VMS_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WSDL = os.path.join(VMS_HOME, "venv-adapter", "lib",
    f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages", "onvif", "wsdl")
PULL_NS = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
EVENT_TOPIC = f"{config.TOPIC_PREFIX}/event"
REGION = config.AWS_REGION

# recordingMode -> the ONVIF topic that drives it (onvif_events.py). All three were confirmed
# firing and latching on cam-02 in Phase 0; another camera may advertise only some of them.
from onvif_events import MODE_TOPICS, camera_topics  # noqa: E402
import aiohttp  # noqa: E402  -- the onvif library's transport; its errors surface from pulls

# Subscription hygiene (FoundAndFixed.md #64). A subscription gets a lifetime and is renewed
# well inside it; a pull asks for a short wait and has its own deadline, because cam-03 held
# PT5S/PT10S pulls open indefinitely; a pull that hangs is "nothing yet", not a lost
# subscription; and every subscription is unsubscribed when it is given up. cam-03 has 10 slots
# and the old loop, re-subscribing every ~100 s and never unsubscribing, filled them all.
SUB_LIFETIME = "PT600S"
RENEW_EVERY_SEC = 20       # cam-03 grants 600 s at creation, but any Renew only +60 s, and its
                           # subscriptions died after 60 s in practice: renew well inside that
PULL_TIMEOUT = "PT2S"
PULL_DEADLINE_SEC = 8
PULL_PACE_SEC = 1.0        # cam-03 answers at once, whatever Timeout says: don't spin
MAX_HUNG_PULLS = 3
TOPIC_RECHECK_SEC = 300
COOLDOWN_SEC = 60          # Phase 0: 60 s -> ~1.6 clips/h overall, 0.22/h overnight
# clip_to_s3 cuts ts+33s, and KVS only returns already-ingested footage, so the publish
# has to wait for that window to actually elapse. Measured: publishing immediately gave
# 12 s of media instead of 45. Small margin over 33 s for fragment boundaries.
PUBLISH_DELAY_SEC = 38
REGISTRY_POLL_SEC = 60     # how quickly a GUI mode change takes effect


def log(msg):
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


class ClipGate:
    """Decides whether a detection state-change should produce a clip.

    Pulled out of the event loop so it can be replayed against recorded event logs
    (see adapter/bin/replay-gate.py) rather than only tested by standing in front of a camera.
    Two rules, both measured in Phase 0:

    * fire on the *rising* edge only -- the detectors latch (true ... false, ~5 s apart),
      so acting on every message would double-count every incident;
    * enforce a cooldown -- one person crossing the hall produced dozens of rising edges,
      and each one would otherwise cost a 45-second clip, a GetClip and an S3 PUT.
    """

    def __init__(self, cooldown_sec=COOLDOWN_SEC):
        self.cooldown = cooldown_sec
        self.asserted = False
        self.last_fire = None

    def update(self, state: bool, now: float):
        """Returns (should_fire, reason)."""
        if not state:
            self.asserted = False
            return False, "cleared"
        if self.asserted:
            return False, "already asserted"
        self.asserted = True
        if self.last_fire is not None and (now - self.last_fire) < self.cooldown:
            return False, f"cooldown ({now - self.last_fire:.0f}s < {self.cooldown}s)"
        self.last_fire = now
        return True, "fire"


def load_cameras():
    """Registry rows for cameras whose recordingMode is a detection mode, unless unregistered."""
    items = get_session(REGION).resource("dynamodb").Table("cameras").scan()["Items"]
    return {
        i["cameraId"]: i for i in items
        if i.get("recordingMode") in MODE_TOPICS and i.get("onvifHost") and not is_unregistered(i)
    }


async def publish_event(camera_id, mode, detected_at, dry_run):
    """One detection -> one clip, via the existing IoT Rule.

    Waits out the post-roll before publishing. `clip_to_s3.py` cuts
    `ts-12s .. ts+33s`, and KVS can only return footage it has already ingested -- so
    publishing at detection time yields only the pre-roll. Measured: a trigger sent at
    ts=now produced **12.0 s** of actual media against the 45 s the clip metadata
    claims, i.e. the approach to the event but none of the event itself. Delaying the
    publish until the window has actually happened produced 42 s.

    The timestamp in the payload stays the *detection* moment, so the clip is still
    centred on the event; only the write is deferred.
    """
    await asyncio.sleep(0 if dry_run else PUBLISH_DELAY_SEC)
    payload = {
        "timestamp": detected_at.isoformat(),
        "stream": camera_id,
        "labels": [mode],
    }
    if dry_run:
        log(f"  [dry-run] would publish (after {PUBLISH_DELAY_SEC}s post-roll) {payload}")
        return
    try:
        get_session(REGION).client("iot-data").publish(
            topic=EVENT_TOPIC, qos=1, payload=json.dumps(payload))
        log(f"  PUBLISHED clip trigger: {payload}")
    except Exception as e:
        log(f"  PUBLISH FAILED ({type(e).__name__}: {str(e)[:120]}) -- clip lost for {detected_at}")


async def watch_camera(item, dry_run, stop_event):
    """Hold a PullPoint subscription for one camera and fire on rising edges."""
    from onvif import ONVIFCamera

    cam_id = item["cameraId"]
    mode = item["recordingMode"]
    topic = MODE_TOPICS[mode]
    gate = ClipGate()
    warned_topic = False

    while not stop_event.is_set():
        sm = cam = None
        try:
            cam = ONVIFCamera(item["onvifHost"], int(item.get("onvifPort", 80)),
                              item["onvifUser"], item["onvifPassword"], wsdl_dir=WSDL)
            await cam.update_xaddrs()

            # Subscribe only to a topic the camera advertises: otherwise nothing can ever fire,
            # and the GUIs offered the mode by mistake.
            topics = await camera_topics(cam)
            if topic not in topics:
                if not warned_topic:
                    log(f"[{cam_id}] mode={mode} needs {topic}, which this camera doesn't publish "
                        f"(it offers: {', '.join(sorted(topics)) or 'nothing'}) -- not subscribing")
                    warned_topic = True
                await _close(None, cam)
                await asyncio.sleep(TOPIC_RECHECK_SEC)
                continue

            ev = await cam.create_events_service()
            sub = await ev.CreatePullPointSubscription({"InitialTerminationTime": SUB_LIFETIME})
            cam.xaddrs[PULL_NS] = sub.SubscriptionReference.Address._value_1
            pull = await cam.create_pullpoint_service()
            sm = await cam.create_subscription_service("PullPointSubscription")
            log(f"[{cam_id}] subscribed, mode={mode}, topic={topic}")
            renewed = asyncio.get_event_loop().time()
            hung = 0

            while not stop_event.is_set():
                now = asyncio.get_event_loop().time()
                if now - renewed >= RENEW_EVERY_SEC:
                    await asyncio.wait_for(sm.Renew({"TerminationTime": SUB_LIFETIME}), PULL_DEADLINE_SEC)
                    renewed = now
                started = asyncio.get_event_loop().time()
                try:
                    msgs = await asyncio.wait_for(
                        pull.PullMessages({"Timeout": PULL_TIMEOUT, "MessageLimit": 50}),
                        PULL_DEADLINE_SEC)
                    hung = 0
                except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                    # A pull that hangs, or a keep-alive connection the camera dropped
                    # ("Server disconnected"), is "nothing yet": pull again on the same
                    # subscription. Only a run of them means it is really gone.
                    hung += 1
                    if hung >= MAX_HUNG_PULLS:
                        raise RuntimeError(f"{hung} pulls in a row failed ({type(e).__name__})")
                    continue
                notes = getattr(msgs, "NotificationMessage", None) or []
                if not notes and asyncio.get_event_loop().time() - started < PULL_PACE_SEC:
                    await asyncio.sleep(PULL_PACE_SEC)
                for m in notes:
                    try:
                        if str(m.Topic._value_1) != topic:
                            continue
                    except Exception:
                        continue

                    state = None
                    try:
                        for si in m.Message._value_1.Data.SimpleItem:
                            name = getattr(si, "Name", None) or si.get("Name")
                            val = getattr(si, "Value", None) or si.get("Value")
                            if name in ("State", "IsMotion"):
                                state = str(val).lower() == "true"
                    except Exception:
                        pass
                    if state is None:
                        continue

                    fire, reason = gate.update(state, asyncio.get_event_loop().time())
                    if fire:
                        detected_at = datetime.now(timezone.utc)
                        log(f"[{cam_id}] detection at {detected_at.isoformat()} -> clip "
                            f"(publishing in {PUBLISH_DELAY_SEC}s, after post-roll)")
                        # background task: the pull loop must keep draining events during
                        # the post-roll wait, or the latch clear would be missed
                        asyncio.create_task(
                            publish_event(cam_id, mode, detected_at, dry_run))
                    elif reason.startswith("cooldown"):
                        log(f"[{cam_id}] detection suppressed: {reason}")
        except asyncio.CancelledError:
            await _close(sm, cam)
            raise
        except Exception as e:
            # Subscriptions expire, cameras reboot, the LAN blips. Log it so a gap is
            # explainable, then rebuild rather than dying -- after giving the old one back.
            log(f"[{cam_id}] subscription lost ({type(e).__name__}: {str(e)[:120]}), retrying in 10s")
            await _close(sm, cam)
            await asyncio.sleep(10)
        else:
            await _close(sm, cam)


async def _close(sm, cam):
    """Give the subscription back and close the camera's HTTP session, best effort. A camera
    has only so many subscriptions (cam-03: 10), and an unclosed session is a leaked socket
    (the "Unclosed client session" lines this loop used to log)."""
    if sm is not None:
        try:
            await asyncio.wait_for(sm.Unsubscribe(), PULL_DEADLINE_SEC)
        except Exception:
            pass
    if cam is not None:
        try:
            await cam.close()
        except Exception:
            pass


async def main(dry_run):
    stop_event = asyncio.Event()
    tasks = {}
    log(f"event watcher starting (dry_run={dry_run}, cooldown={COOLDOWN_SEC}s)")

    while True:
        try:
            wanted = load_cameras()
        except Exception as e:
            log(f"registry read failed ({type(e).__name__}: {str(e)[:120]}), keeping current set")
            wanted = {c: t[1] for c, t in tasks.items()}

        # start watchers for newly-enabled cameras
        for cam_id, item in wanted.items():
            prev = tasks.get(cam_id)
            if prev and prev[1].get("recordingMode") == item.get("recordingMode"):
                continue
            if prev:
                log(f"[{cam_id}] mode changed -> restarting watcher")
                prev[0].cancel()
            tasks[cam_id] = (asyncio.create_task(watch_camera(item, dry_run, stop_event)), item)

        # stop watchers for cameras switched back to manual / removed
        for cam_id in [c for c in tasks if c not in wanted]:
            log(f"[{cam_id}] no longer a detection mode -> stopping watcher")
            tasks.pop(cam_id)[0].cancel()

        if not tasks:
            log("no cameras in a detection mode; idling")
        await asyncio.sleep(REGISTRY_POLL_SEC)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="log intended publishes without creating clips")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.dry_run))
    except KeyboardInterrupt:
        pass
