"""ONVIF event topics: which detection modes a camera can actually serve.

Shared by the event watcher (it subscribes only to a topic the camera advertises) and the
admin GUI (it offers only those modes). A mode is a topic: the watcher fires on that topic's
rising edge. The topics are cam-02's; another camera may advertise only some of them -- cam-03
publishes cell motion and nothing else, its boxes around people being an on-screen overlay
(FoundAndFixed.md #64).
"""

MODE_TOPICS = {
    "motion":     "tns1:VideoSource/MotionAlarm",
    "cellMotion": "tns1:RuleEngine/CellMotionDetector/Motion",
    "human":      "tns1:UserAlarm/IVA/HumanShapeDetect",
}

_WSTOP = "{http://docs.oasis-open.org/wsn/t-1}topic"


def topics_from_properties(props) -> set[str]:
    """Every concrete topic in a GetEventProperties response, as "tns1:A/B/C" -- the form
    notifications carry in Topic. Leaves are the elements marked wstop:topic="true"."""
    found = set()

    def walk(el, path):
        name = el.tag.split("}")[-1]
        here = path + [name]
        if el.get(_WSTOP) == "true":
            found.add("tns1:" + "/".join(here))
        for child in el:
            if child.tag.endswith(("MessageDescription", "Documentation")):
                continue
            walk(child, here)

    for top in getattr(props.TopicSet, "_value_1", None) or []:
        walk(top, [])
    return found


def supported_modes(topics: set[str]) -> list[str]:
    """The detection modes whose topic is among `topics`, in MODE_TOPICS order."""
    return [mode for mode, topic in MODE_TOPICS.items() if topic in topics]


async def camera_topics(cam) -> set[str]:
    """Topics advertised by an already-connected ONVIFCamera (update_xaddrs done)."""
    ev = await cam.create_events_service()
    return topics_from_properties(await ev.GetEventProperties())
