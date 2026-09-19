"""MediaMTX control-API access, in one place.

`adapter/onvif-admin/app.py` grew the first copy of these calls; the outage-buffer
supervisor would have been the second. Factored out before that happened.

The API is localhost-only (mediamtx.yml binds 127.0.0.1:9997), so there is no auth here
and none is needed -- reaching it already means local access to the Pi.

**Which fields can be changed in place matters more than it looks.** MediaMTX decides
per-update whether a path config can be applied live or needs the path closed and
recreated (`core.pathConfCanBeUpdated`), and it compares a fixed whitelist of fields.
Recreating a path disconnects every reader -- a running KVS producer included. The record
fields used by the outage buffer ARE in that whitelist; `source` is NOT. So changing a
camera's source URI costs a reconnect and changing its recording settings does not.
"""
import requests

API = "http://127.0.0.1:9997"
TIMEOUT = 5


class MediaMTXError(RuntimeError):
    pass


def get_path_conf(path: str) -> dict | None:
    """Configured settings for a path, or None if it has no explicit entry.

    Returns None rather than raising for the "not found" case, which is normal: a path
    served by the `all_others` catch-all has no config of its own. That is exactly why
    cam01 needs an explicit entry before its record settings can be patched.
    """
    r = requests.get(f"{API}/v3/config/paths/get/{path}", timeout=TIMEOUT)
    if r.status_code == 404 or (r.ok and r.json().get("status") == "error"):
        return None
    if not r.ok:
        raise MediaMTXError(f"get conf {path}: {r.status_code} {r.text}")
    return r.json()


def get_path_state(path: str) -> dict | None:
    """Live runtime state (ready, tracks, readyTime, source), not configuration."""
    r = requests.get(f"{API}/v3/paths/get/{path}", timeout=TIMEOUT)
    if not r.ok:
        return None
    return r.json()


def patch_path(path: str, conf: dict) -> None:
    """Merge `conf` into a path's configuration.

    A merge, not a replace -- fields left out keep their current values. Sending a field
    with the value it already holds is a no-op as far as path restarts are concerned,
    because MediaMTX compares values rather than tracking which keys were present.
    """
    r = requests.patch(f"{API}/v3/config/paths/patch/{path}", json=conf, timeout=TIMEOUT)
    if not r.ok:
        raise MediaMTXError(f"patch {path}: {r.status_code} {r.text}")


def add_path(path: str, conf: dict) -> None:
    r = requests.post(f"{API}/v3/config/paths/add/{path}", json=conf, timeout=TIMEOUT)
    if not r.ok:
        raise MediaMTXError(f"add {path}: {r.status_code} {r.text}")


def ensure_path_conf(path: str, conf: dict) -> None:
    """Create the path config if absent, otherwise patch it.

    cam01 is published into the `all_others` catch-all and has no entry of its own, so a
    plain patch 404s. Adding one with `source: publisher` was verified not to disturb the
    live publisher (readyTime and source id unchanged across the add).
    """
    if get_path_conf(path) is None:
        add_path(path, {"source": "publisher", **conf})
    else:
        patch_path(path, conf)


def set_recording(path: str, on: bool, record_path: str = None,
                  segment_duration: str = "30s", part_duration: str = "1s") -> None:
    """Arm or disarm recording for one path.

    `recordDeleteAfter: 0s` disables MediaMTX's own cleaner permanently and is deliberate:
    the outage supervisor owns retention itself. Letting MediaMTX delete would put the
    captured footage at the mercy of a cleaner tick racing the supervisor -- and the thing
    it would delete is the footage the whole feature exists to save.
    """
    conf = {"record": bool(on)}
    if on:
        conf.update({
            "recordPath": record_path,
            "recordFormat": "fmp4",          # mandatory: mpegts cannot carry LPCM/G711
            "recordSegmentDuration": segment_duration,
            "recordPartDuration": part_duration,
            "recordDeleteAfter": "0s",
        })
    patch_path(path, conf)


def recording_conf_matches(path: str, on: bool, record_path: str = None,
                           segment_duration: str = "30s") -> bool:
    """Whether the live config already says what we want.

    Used by the supervisor's reconcile loop rather than tracking what it last sent:
    MediaMTX does NOT persist API config changes to mediamtx.yml, so a restart silently
    reverts every armed path. Comparing against reality catches that within one tick;
    remembering what we sent would not.
    """
    conf = get_path_conf(path)
    if conf is None:
        return False
    if bool(conf.get("record")) != bool(on):
        return False
    if not on:
        return True
    # MediaMTX reports a disabled cleaner as an EMPTY STRING, not the "0s" that was sent.
    # Comparing against the literal we wrote made this return False on every tick, so the
    # reconcile loop re-patched forever. Harmless at MediaMTX's end (same values compare
    # equal, so the path is not recreated) but it spammed the log and did real work every
    # 5 s. Compare parsed seconds, and treat empty as zero.
    return (conf.get("recordPath") == record_path
            and conf.get("recordFormat") == "fmp4"
            and _dur_secs(conf.get("recordDeleteAfter") or "0s") == 0
            and _dur_eq(conf.get("recordSegmentDuration"), segment_duration))


def _dur_eq(a: str, b: str) -> bool:
    # MediaMTX normalises durations on read: "30s" comes back as "30s" but "2m" becomes
    # "2m0s" and "1h" becomes "1h0m0s". Compare parsed seconds, not strings, or the
    # reconcile loop patches the same value forever.
    return _dur_secs(a) == _dur_secs(b)


def _dur_secs(s) -> float | None:
    if not s:
        return None
    total, num = 0.0, ""
    for ch in str(s):
        if ch.isdigit() or ch == ".":
            num += ch
        else:
            mult = {"h": 3600, "m": 60, "s": 1, "µ": 0, "n": 0}.get(ch)
            if mult is None:
                return None
            total += float(num or 0) * mult
            num = ""
    return total
