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
import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from aws_device_creds import get_session

WSDL = "/home/vladimir/MyProjects/VMS/venv-adapter/lib/python3.13/site-packages/onvif/wsdl"
PULL_NS = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
EVENT_TOPIC = "adapter/adapter-01/event"
REGION = "eu-central-1"

# recordingMode -> the ONVIF topic that drives it. All three were confirmed firing and
# latching in Phase 0; they differ only in what they classify, so one mapping covers
# Phase 2 (motion) and Phase 3 (human) alike.
MODE_TOPICS = {
    "motion":     "tns1:VideoSource/MotionAlarm",
    "cellMotion": "tns1:RuleEngine/CellMotionDetector/Motion",
    "human":      "tns1:UserAlarm/IVA/HumanShapeDetect",
}
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
    """Registry rows for cameras whose recordingMode is a detection mode."""
    items = get_session(REGION).resource("dynamodb").Table("cameras").scan()["Items"]
    return {
        i["cameraId"]: i for i in items
        if i.get("recordingMode") in MODE_TOPICS and i.get("onvifHost")
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

    while not stop_event.is_set():
        try:
            cam = ONVIFCamera(item["onvifHost"], int(item.get("onvifPort", 80)),
                              item["onvifUser"], item["onvifPassword"], wsdl_dir=WSDL)
            await cam.update_xaddrs()
            ev = await cam.create_events_service()
            sub = await ev.CreatePullPointSubscription()
            cam.xaddrs[PULL_NS] = sub.SubscriptionReference.Address._value_1
            pull = await cam.create_pullpoint_service()
            log(f"[{cam_id}] subscribed, mode={mode}, topic={topic}")

            while not stop_event.is_set():
                msgs = await pull.PullMessages({"Timeout": "PT10S", "MessageLimit": 50})
                for m in (getattr(msgs, "NotificationMessage", None) or []):
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
        except Exception as e:
            # Subscriptions expire, cameras reboot, the LAN blips. Log it so a gap is
            # explainable, then rebuild rather than dying.
            log(f"[{cam_id}] subscription lost ({type(e).__name__}: {str(e)[:120]}), retrying in 10s")
            await asyncio.sleep(10)


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
