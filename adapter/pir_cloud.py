"""The PIR watcher's cloud side: the status item and the clip index in pir-local
(PIR-MQTT-VMS-PI4.md §3.10-§3.11, Phase 10).

pir-local is this project's own table (PK cameraId, SK sk):
  sk = "status"            one per camera: what the Pi sees, for this project's cloud page
  sk = "clip#<startTs>"    one per local clip, so the page can list clips that never left the Pi

**Reconciled, not event-driven.** Every pass compares what each row should say with what was
last written successfully (kept in ~/.local/state/vms/pir-cloud-sync.json) and writes only the
difference. A Wi-Fi drop (FoundAndFixed.md #52), an outage or a restart therefore loses
nothing: the next good pass catches up. The writes run on this module's own thread with short
timeouts, never on the watcher's event path -- outage_buffer.py's _fetch_registry() records
what boto3's 60 s defaults did to a loop that had to stay responsive.

**One writer per field** (§3.11): this module writes the clip details, thumbnails, `kept`,
`deletedAt` and `ttl`; pir-control writes `uploadRequestedAt`/`requestedBy`; the uploader
writes `uploadedKey`/`uploadedAt`/`uploadError`. Always UpdateItem with explicit fields, never
PutItem over an existing row, which would erase the other writers' fields.
"""
import base64
import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

TABLE = "pir-local"
STATUS_MIN_GAP_SEC = 5          # a change is written at most once per 5 s per camera
STATUS_HEARTBEAT_SEC = 300      # and the item every 5 min regardless: silence means "Pi gone"
INDEX_PASS_SEC = 30
SESSION_MAX_AGE_SEC = 1800      # the role-alias token lives 3600 s; refresh well inside it
DELETED_TTL_SEC = 86400         # a deleted clip's row disappears a day later (§3.11)
OFFLINE_BACKOFF_SEC = 10
STATE_FILE = Path.home() / ".local" / "state" / "vms" / "pir-cloud-sync.json"


def _iso(ts=None):
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).isoformat()


def clip_start_ts(journal):
    """The index row's startTs -- deliberately the exact string outage_uploader.py writes into
    the clips table for the same clip, so the two rows can be matched. Local time with offset,
    as every clips row has; the one cost is that the hour after a DST change sorts out of order."""
    return datetime.fromtimestamp(float(journal["from"])).astimezone().isoformat()


def _ddb(value):
    """DynamoDB's resource API takes Decimal, not float; None inside a map is left out."""
    if isinstance(value, float):
        return Decimal(str(round(value, 3)))
    if isinstance(value, dict):
        return {k: _ddb(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_ddb(v) for v in value]
    return value


def _digest(fields):
    return hashlib.sha256(json.dumps(fields, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _set_expression(fields):
    return ("SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            {f"#{k}": k for k in fields},
            {f":{k}": _ddb(v) for k, v in fields.items()})


class CloudSync(threading.Thread):
    def __init__(self, pir_dir, region, log):
        super().__init__(daemon=True, name="pir-cloud")
        self.pir_dir, self.region, self.log = pir_dir, region, log
        self._lock = threading.Lock()
        self._status = {}               # cameraId -> the latest status from the watcher
        self._sent = {}                 # cameraId -> (digest, monotonic time) of the last write
        self._session, self._session_at = None, 0.0
        self._adopted = False
        self.ok, self.last_ok_at, self.last_error = None, None, None
        try:
            self.rows = json.loads(STATE_FILE.read_text()).get("rows", {})
        except Exception:
            self.rows = {}              # sessionId -> {"cameraId", "startTs", "digest"}

    # --- called from the watcher's thread -------------------------------------------------------
    def set_status(self, camera_id, status):
        with self._lock:
            self._status[camera_id] = status

    def health(self):
        """For the local heartbeat file: is the cloud side keeping up?"""
        return {"ok": self.ok, "lastOkAt": self.last_ok_at, "lastError": self.last_error,
                "indexedClips": len(self.rows)}

    # --- the thread -----------------------------------------------------------------------------
    def run(self):
        last_index = 0.0
        while True:
            try:
                self._push_status()
                if time.monotonic() - last_index >= INDEX_PASS_SEC:
                    if not self._adopted:
                        self._adopt_remote_rows()
                    self._reconcile_index()
                    last_index = time.monotonic()
                if self.ok is not True:
                    self.log("cloud sync: pir-local " + ("reachable again" if self.ok is False else "OK"))
                self.ok, self.last_ok_at, self.last_error = True, _iso(), None
                time.sleep(1)
            except Exception as e:  # noqa: BLE001 -- offline is normal here; say so once per streak
                err = f"{type(e).__name__}: {str(e)[:120]}"
                if self.ok is not False:
                    self.log(f"cloud sync failed ({err}) -- retrying; the next good pass catches up")
                self.ok, self.last_error, self._session = False, err, None
                time.sleep(OFFLINE_BACKOFF_SEC)

    def _table(self):
        if self._session is None or time.monotonic() - self._session_at > SESSION_MAX_AGE_SEC:
            from aws_device_creds import get_session
            self._session, self._session_at = get_session(self.region), time.monotonic()
        from botocore.config import Config
        cfg = Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
        return self._session.resource("dynamodb", config=cfg).Table(TABLE)

    # --- §3.10: the status item -----------------------------------------------------------------
    def _push_status(self):
        with self._lock:
            pending = dict(self._status)
        now = time.monotonic()
        for cam, status in pending.items():
            digest, last = _digest(status), self._sent.get(cam)
            due_change = (last is None or last[0] != digest) and (last is None or now - last[1] >= STATUS_MIN_GAP_SEC)
            due_beat = last is not None and now - last[1] >= STATUS_HEARTBEAT_SEC
            if not (due_change or due_beat):
                continue
            expr, names, values = _set_expression({**status, "updatedAt": _iso()})
            self._table().update_item(Key={"cameraId": cam, "sk": "status"}, UpdateExpression=expr,
                                      ExpressionAttributeNames=names, ExpressionAttributeValues=values)
            self._sent[cam] = (digest, now)

    # --- §3.11: the clip index ------------------------------------------------------------------
    def _desired_rows(self):
        """What each local clip's row should say, read from the session folders on the stick."""
        rows = {}
        for path in (self.pir_dir.glob("*/state.json") if self.pir_dir.is_dir() else []):
            try:
                j = json.loads(path.read_text())
            except Exception:
                continue
            if j.get("status") != "merged":
                continue                    # recording, assembling or failed: nothing to offer
            sdir = path.parent
            thumbs = []
            for name in j.get("thumbnails") or []:
                try:
                    thumbs.append(base64.b64encode((sdir / name).read_bytes()).decode())
                except OSError:
                    pass
            start = clip_start_ts(j)
            rows[j["sessionId"]] = {"cameraId": j["cameraId"], "startTs": start, "fields": {
                "sessionId": j["sessionId"], "startTs": start, "seq": j.get("seq"),
                "durationSec": j.get("durationSec"), "window": j.get("window"),
                "sizeBytes": j.get("sizeBytes"), "videoCodec": j.get("videoCodec"),
                "reason": j.get("reason"), "labels": ["pir", f"pir:seq {j.get('seq')}"],
                "thumbnails": thumbs, "kept": (sdir / "keep").exists()}}
        return rows

    def _adopt_remote_rows(self):
        """Once per start: take over live rows this process has no record of (a lost state file,
        a reinstall), so a clip deleted meanwhile still gets its deletedAt."""
        from boto3.dynamodb.conditions import Attr, Key
        cams = set(self._status) | {r["cameraId"] for r in self.rows.values()}
        table = self._table()
        for cam in cams:
            kwargs = {"KeyConditionExpression": Key("cameraId").eq(cam) & Key("sk").begins_with("clip#"),
                      "FilterExpression": Attr("deletedAt").not_exists(),
                      "ProjectionExpression": "sk, sessionId"}
            while True:
                r = table.query(**kwargs)
                for item in r["Items"]:
                    if item.get("sessionId") and item["sessionId"] not in self.rows:
                        self.rows[item["sessionId"]] = {"cameraId": cam, "startTs": item["sk"][len("clip#"):],
                                                        "digest": None}
                if "LastEvaluatedKey" not in r:
                    break
                kwargs["ExclusiveStartKey"] = r["LastEvaluatedKey"]
        self._adopted = True

    def _reconcile_index(self):
        from botocore.exceptions import ClientError
        desired = self._desired_rows()
        for sid, row in desired.items():
            digest = _digest(row["fields"])
            if self.rows.get(sid, {}).get("digest") == digest:
                continue
            expr, names, values = _set_expression(row["fields"])
            self._table().update_item(
                Key={"cameraId": row["cameraId"], "sk": f"clip#{row['startTs']}"},
                UpdateExpression=expr + " REMOVE deletedAt, #ttl",
                ExpressionAttributeNames={**names, "#ttl": "ttl"}, ExpressionAttributeValues=values)
            self.rows[sid] = {"cameraId": row["cameraId"], "startTs": row["startTs"], "digest": digest}
            self._save()
        # A clip that left the stick (retention, or Delete in the admin page): mark its row so the
        # cloud page stops offering it, and let DynamoDB's TTL remove it a day later.
        for sid in [s for s in self.rows if s not in desired]:
            r = self.rows[sid]
            try:
                self._table().update_item(
                    Key={"cameraId": r["cameraId"], "sk": f"clip#{r['startTs']}"},
                    UpdateExpression="SET deletedAt = :d, #ttl = :t",
                    ConditionExpression="attribute_exists(sk)",   # never resurrect an expired row
                    ExpressionAttributeNames={"#ttl": "ttl"},
                    ExpressionAttributeValues={":d": _iso(), ":t": int(time.time()) + DELETED_TTL_SEC})
                self.log(f"cloud index: {sid} marked deleted; its row expires in a day")
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
            del self.rows[sid]
            self._save()

    def _save(self):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({"rows": self.rows}, indent=1))
        tmp.replace(STATE_FILE)
