"""Shared AWS-reachability state between agent.py (writer) and outage_buffer.py (reader).

`agent.py` holds the only MQTT connection this adapter is allowed (the IoT policy grants
`iot:Connect` on a single client id), so it is the only process that can see
`on_connection_interrupted` / `on_connection_resumed`. The outage supervisor is a separate
unit -- deliberately, for the reason kvs-event-watcher.service already documents: if one
wedges, the other must keep working. A small file is the whole IPC.

**This is a heartbeat, not a transition log, and that distinction is load-bearing.** A
file that only changes on transitions is indistinguishable from a healthy connection when
the writer dies: agent.py crashes, `on_connection_interrupted` never fires, the file stops
changing, and a naive reader concludes "still online" forever. So it is rewritten every
HEARTBEAT_SEC regardless, and a stale file means *unknown* -- never *online*, and never
*outage* either (see outage_buffer.py on why a crashed agent must not trigger recording).
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_SEC = 10
STALE_AFTER_SEC = HEARTBEAT_SEC * 3


def _runtime_dir() -> Path:
    # XDG_RUNTIME_DIR is set for systemd *user* units, which both writer and reader are.
    # Falling back to /run/user/<uid> rather than /tmp keeps it off any world-writable
    # path, and both processes resolve it identically.
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "vms"


STATE_FILE = _runtime_dir() / "aws-state.json"


def write_state(online: bool, since: str = None, seq: int = 0) -> None:
    """Atomically publish current reachability. Safe to call every heartbeat."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "online": bool(online),
        "since": since or datetime.now(timezone.utc).isoformat(),
        "ts": datetime.now(timezone.utc).isoformat(),
        "seq": seq,
        "pid": os.getpid(),
    }
    # tmp + fsync + rename: a reader polling every few seconds must never observe a
    # half-written file, and a torn read here would be interpreted as a state change.
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, prefix=".aws-state-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_state() -> dict:
    """Return {'online': bool|None, 'stale': bool, 'age': float, ...}.

    `online` is None when the answer is genuinely unknown -- no file, unparseable, or a
    heartbeat too old. Callers must distinguish that from False.
    """
    try:
        raw = json.loads(STATE_FILE.read_text())
        ts = datetime.fromisoformat(raw["ts"])
        age = (datetime.now(timezone.utc) - ts).total_seconds()
        stale = age > STALE_AFTER_SEC
        return {
            "online": None if stale else bool(raw.get("online")),
            "stale": stale,
            "age": age,
            "since": raw.get("since"),
            "seq": raw.get("seq", 0),
        }
    except Exception:
        return {"online": None, "stale": True, "age": float("inf"), "since": None, "seq": 0}
