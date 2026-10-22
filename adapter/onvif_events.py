"""ONVIF event topics: which detection modes a camera can actually serve, and on which topic.

Shared by discovery (the device card says which recording triggers the camera offers), the
admin GUI (registration and the table record them; the selectors offer only those) and the
event watcher (it subscribes to the camera's own topic for the mode and reads its own field).

A recording mode is one of the four values the registry shares with the successor
(PIR-MQTT-VMS-PI4.md §4.3: no new enum values). Which ONVIF topic serves it is per camera,
because vendors name the same detection differently: cam-02's human detection is
`UserAlarm/IVA/HumanShapeDetect`, other firmware uses `RuleEngine/.../People...` names. So a
camera's map is found from its own GetEventProperties: the exact names known here first, then the
topic's wording, keeping only topics whose payload has an on/off field to fire on.

Two limits no topic list can remove (FoundAndFixed.md #64): an advertised topic may stay silent
until the camera's own configuration enables the detection (cam-03's cell motion needs motion
detection ticked in its web interface), and a camera may detect without telling ONVIF at all
(cam-03's human intrusion alarm). The map says what the camera *offers*; a walk past it proves it.
"""
import re

# The topics confirmed on hardware here (cam-02, Phase 0), as each mode's first choice.
MODE_TOPICS = {
    "motion":     "tns1:VideoSource/MotionAlarm",
    "cellMotion": "tns1:RuleEngine/CellMotionDetector/Motion",
    "human":      "tns1:UserAlarm/IVA/HumanShapeDetect",
}

# Wording that identifies a topic for a mode when its exact name is not one of the above.
# Checked against the full topic path, in order: cell motion before plain motion, so a cell-motion
# topic is never taken for "motion", and human words before motion words.
_MODE_WORDS = [
    ("cellMotion", re.compile(r"CellMotion", re.I)),
    ("human",      re.compile(r"Human|People|Person|Pedestrian", re.I)),
    ("motion",     re.compile(r"Motion", re.I)),
]
# Data fields that carry the on/off state, in the order they are preferred.
_STATE_FIELDS = ("State", "IsMotion", "IsPeople", "IsHuman", "IsPerson", "IsInside", "LogicalState",
                 "Value")
_ON_OFF_TYPES = ("boolean", "RelayLogicalState")

_WSTOP = "{http://docs.oasis-open.org/wsn/t-1}topic"


def _local(tag):
    return tag.split("}")[-1]


def topic_details(props) -> dict[str, list[tuple[str, str]]]:
    """{"tns1:A/B/C": [(data field name, type), ...]} for every concrete topic in a
    GetEventProperties response. Topics are the elements marked wstop:topic="true"; their data
    fields are the SimpleItemDescriptions under MessageDescription/Data."""
    found = {}

    def walk(el, path):
        here = path + [_local(el.tag)]
        if el.get(_WSTOP) == "true":
            fields = []
            for md in el.iter():
                if _local(md.tag) != "Data":
                    continue
                for item in md.iter():
                    if _local(item.tag) == "SimpleItemDescription":
                        fields.append((item.get("Name"), (item.get("Type") or "").split(":")[-1]))
            found["tns1:" + "/".join(here)] = fields
        for child in el:
            if _local(child.tag) in ("MessageDescription", "Documentation"):
                continue
            walk(child, here)

    for top in getattr(props.TopicSet, "_value_1", None) or []:
        walk(top, [])
    return found


def topics_from_properties(props) -> set[str]:
    return set(topic_details(props))


def _state_field(fields):
    """The on/off field of a topic's payload, or None when it has none to fire on."""
    names = {n: t for n, t in fields}
    for name in _STATE_FIELDS:
        if name in names and names[name] in _ON_OFF_TYPES:
            return name
    for name, typ in fields:
        if typ in _ON_OFF_TYPES:
            return name
    return None


def detection_map(details: dict[str, list[tuple[str, str]]]) -> dict[str, dict]:
    """{mode: {"topic", "field", "match": "known"|"wording"}} for the modes this camera can serve.

    Known topic names first; otherwise the first topic, in sorted order, whose wording names the
    mode and whose payload has an on/off field. A topic serves one mode only."""
    out, used = {}, set()
    for mode, topic in MODE_TOPICS.items():
        if topic in details and _state_field(details[topic]):
            out[mode] = {"topic": topic, "field": _state_field(details[topic]), "match": "known"}
            used.add(topic)
    for topic in sorted(details):
        if topic in used:
            continue
        field = _state_field(details[topic])
        if not field:
            continue
        for mode, words in _MODE_WORDS:
            if words.search(topic):
                if mode not in out:
                    out[mode] = {"topic": topic, "field": field, "match": "wording"}
                    used.add(topic)
                break
    return {mode: out[mode] for mode in MODE_TOPICS if mode in out}


def supported_modes(topics) -> list[str]:
    """The detection modes a camera serves. Accepts a detection_map() result, or (as before) a
    plain set of topic names, judged by exact name only."""
    if isinstance(topics, dict):
        return [m for m in MODE_TOPICS if m in topics]
    return [mode for mode, topic in MODE_TOPICS.items() if topic in topics]


async def camera_detection(cam) -> tuple[dict, set[str]]:
    """(detection_map, all topics) of an already-connected ONVIFCamera (update_xaddrs done)."""
    ev = await cam.create_events_service()
    details = topic_details(await ev.GetEventProperties())
    return detection_map(details), set(details)


async def camera_topics(cam) -> set[str]:
    return (await camera_detection(cam))[1]


# --- subscriptions ------------------------------------------------------------------------------
#
# Subscription hygiene in one place, for the event watcher and the admin GUI's trigger test
# (FoundAndFixed.md #64). A subscription gets a lifetime and is renewed well inside it; a pull asks
# for a short wait and has its own deadline, because cam-03 held PT5S/PT10S pulls open
# indefinitely; a pull that hangs, or a keep-alive connection the camera dropped, is "nothing
# yet", not a lost subscription; and every subscription is given back when it ends. cam-03 has 10
# slots, and the old watcher loop, re-subscribing every ~100 s and never unsubscribing, filled them.

PULL_NS = "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription"
SUB_LIFETIME = "PT600S"
RENEW_EVERY_SEC = 20       # cam-03 grants 600 s at creation, but any Renew only +60 s, and its
                           # subscriptions died after 60 s in practice: renew well inside that
PULL_TIMEOUT = "PT2S"
PULL_DEADLINE_SEC = 8
PULL_PACE_SEC = 1.0        # cam-03 answers at once, whatever Timeout says: don't spin
MAX_HUNG_PULLS = 3


class SubscriptionLost(RuntimeError):
    pass


class PullSubscription:
    """One PullPoint subscription on an already-connected ONVIFCamera:

        async with PullSubscription(cam) as sub:
            while ...:
                for note in await sub.pull():
                    ...

    pull() renews when due, returns [] for a hang or a dropped connection, paces itself when the
    camera answers at once, and raises SubscriptionLost after MAX_HUNG_PULLS failures in a row.
    Leaving the block unsubscribes, best effort, on every path (cancellation included)."""

    def __init__(self, cam):
        self.cam = cam
        self.pullpoint = self.manager = None
        self.address = None
        self._renewed = 0.0
        self._failed = 0

    async def __aenter__(self):
        import asyncio
        ev = await self.cam.create_events_service()
        sub = await ev.CreatePullPointSubscription({"InitialTerminationTime": SUB_LIFETIME})
        self.address = sub.SubscriptionReference.Address._value_1
        self.cam.xaddrs[PULL_NS] = self.address
        self.pullpoint = await self.cam.create_pullpoint_service()
        self.manager = await self.cam.create_subscription_service("PullPointSubscription")
        self._renewed = asyncio.get_event_loop().time()
        return self

    async def __aexit__(self, *exc):
        import asyncio
        if self.manager is not None:
            try:
                await asyncio.wait_for(self.manager.Unsubscribe(), PULL_DEADLINE_SEC)
            except Exception:  # noqa: BLE001 -- best effort; it expires on its own
                pass
        return False

    async def pull(self):
        import asyncio
        import aiohttp      # the onvif library's transport; its errors surface from pulls
        loop = asyncio.get_event_loop()
        if loop.time() - self._renewed >= RENEW_EVERY_SEC:
            await asyncio.wait_for(self.manager.Renew({"TerminationTime": SUB_LIFETIME}),
                                   PULL_DEADLINE_SEC)
            self._renewed = loop.time()
        started = loop.time()
        try:
            msgs = await asyncio.wait_for(
                self.pullpoint.PullMessages({"Timeout": PULL_TIMEOUT, "MessageLimit": 50}),
                PULL_DEADLINE_SEC)
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            self._failed += 1
            if self._failed >= MAX_HUNG_PULLS:
                raise SubscriptionLost(f"{self._failed} pulls in a row failed ({type(e).__name__})")
            return []
        self._failed = 0
        notes = getattr(msgs, "NotificationMessage", None) or []
        if not notes and loop.time() - started < PULL_PACE_SEC:
            await asyncio.sleep(PULL_PACE_SEC)
        return notes


def parse_notification(note):
    """(topic, PropertyOperation or None, {data field: value}) of one NotificationMessage; the
    data dict is empty when the payload can't be read."""
    try:
        topic = str(note.Topic._value_1)
    except Exception:  # noqa: BLE001
        return None, None, {}
    op, data = None, {}
    try:
        msg = note.Message._value_1
        op = getattr(msg, "PropertyOperation", None)
        for si in (msg.Data.SimpleItem or []):
            name = getattr(si, "Name", None) or si.get("Name")
            data[name] = getattr(si, "Value", None) or si.get("Value")
    except Exception:  # noqa: BLE001
        pass
    return topic, op, data


def state_value(data: dict, field: str):
    """The on/off value of one notification's data, or None when it carries none. The declared
    field first; otherwise the first known state field present, for a camera whose events don't
    use the name its event list declares: cam-04's MotionAlarm is declared with State and sent
    with IsMotion (FoundAndFixed.md #67), and matching only the declared name dropped every one."""
    if field in data:
        return data[field]
    return next((data[name] for name in _STATE_FIELDS if name in data), None)


def is_on(value) -> bool:
    """A state value that means "detecting": boolean true, or a relay's "active"."""
    return str(value).lower() in ("true", "active", "1")
