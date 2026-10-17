"""ONVIF Media2 (ver20/media), over raw SOAP: just enough to read and switch the video codec of
the encoder behind a camera's registered RTSP stream.

Raw SOAP, not the `onvif` library, because that library ships Media1 WSDLs only, and Media1's
encoding enum stops at H.264: a camera's H.265 capability is invisible through it (guide §21.4,
measurements/codec-phase0.md §2). cam-03 shows the gap exactly -- Media1 lists both of its
encoders as H264, Media2 offers H264 and H265 on both.

What a camera *reports* about its encoder is not proof of what it sends. cam-03 read back
H264 2880x1620 @25 while MediaMTX received H.265 2560x1440 @20 from the same profile's URI. So
the admin GUI treats a switch as done only when MediaMTX receives the new codec
(mediamtx_api.path_video_codec), and this module never claims more than "the camera accepted
the setting".
"""
import base64
import copy
import datetime as dt
import hashlib
import os
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import escape

import requests
from lxml import etree

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
}
MEDIA2_NS = NS["tr2"]
TIMEOUT = 8

# ONVIF's encoding names (tt:VideoEncodingMimeNames) <-> this project's codec names, the ones
# stream-codec.py, the registry and the clips table use.
ENCODING = {"h264": "H264", "h265": "H265"}
CODEC = {v: k for k, v in ENCODING.items()}

# Preferred profile when the current one isn't valid for the target codec. H.264 High first:
# Baseline once played in ffmpeg and stayed black in browsers (guide §16.1). Main for H.265,
# because browsers decode Main and not reliably Main10.
PROFILE_PREFERENCE = {"H264": ["High", "Main", "Baseline"], "H265": ["Main", "Main10"]}

# Frame rates the admin GUI offers, each only where the stream's encoder accepts it. 10 is what
# the registration form preselects; a camera already registered keeps its rate until changed.
FPS_CHOICES = (10, 15, 20, 25)
DEFAULT_FPS = 10


class OnvifError(RuntimeError):
    pass


def _q(tag):
    prefix, name = tag.split(":")
    return f"{{{NS[prefix]}}}{name}"


class Media2:
    """One camera's device + Media2 services. Credentials come from the registry row."""

    def __init__(self, host, port, user, password, timeout=TIMEOUT):
        self.host, self.port = host, int(port or 80)
        self.user, self.password = user, password
        self.timeout = timeout
        self.device_url = f"http://{self.host}:{self.port}/onvif/device_service"
        self._offset = None        # camera clock minus ours, for the digest's Created
        self._media2_url = None

    # --- transport -----------------------------------------------------------------------

    def _security(self):
        nonce = os.urandom(16)
        created = (dt.datetime.now(dt.timezone.utc) + (self._offset or dt.timedelta())
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(
            nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            '<wsse:Security s:mustUnderstand="1" '
            'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
            'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
            f'<wsse:UsernameToken><wsse:Username>{escape(self.user)}</wsse:Username>'
            '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
            '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/'
            f'oasis-200401-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}'
            f'</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>')

    def _call(self, url, body, auth=True):
        """POST one SOAP 1.2 request; return the parsed Body's first child. Faults raise."""
        env = (f'<s:Envelope xmlns:s="{NS["s"]}" xmlns:tds="{NS["tds"]}" xmlns:tr2="{NS["tr2"]}" '
               f'xmlns:tt="{NS["tt"]}"><s:Header>{self._security() if auth else ""}</s:Header>'
               f'<s:Body>{body}</s:Body></s:Envelope>')
        try:
            r = requests.post(url, data=env.encode(), timeout=self.timeout,
                              headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        except requests.RequestException as e:
            raise OnvifError(f"{self.host} unreachable ({type(e).__name__})") from e
        try:
            root = etree.fromstring(r.content)
        except etree.XMLSyntaxError as e:
            raise OnvifError(f"HTTP {r.status_code}, not a SOAP reply") from e
        fault = root.find(".//s:Fault", NS)
        if fault is not None:
            reason = " ".join(t.strip() for t in fault.itertext() if t.strip())
            raise OnvifError(f"camera refused: {reason[:300]}")
        body_el = root.find("s:Body", NS)
        if body_el is None or not len(body_el):
            raise OnvifError(f"HTTP {r.status_code}, empty SOAP body")
        return body_el[0]

    def _sync_clock(self):
        """Digest auth carries a timestamp, and cameras refuse one far off their own clock --
        their clocks drift and many never see NTP. GetSystemDateAndTime needs no auth."""
        if self._offset is not None:
            return
        self._offset = dt.timedelta()
        try:
            resp = self._call(self.device_url, "<tds:GetSystemDateAndTime/>", auth=False)
            u = resp.find(".//tt:UTCDateTime", NS)
            parts = [int(u.findtext(p, namespaces=NS)) for p in
                     ("tt:Date/tt:Year", "tt:Date/tt:Month", "tt:Date/tt:Day",
                      "tt:Time/tt:Hour", "tt:Time/tt:Minute", "tt:Time/tt:Second")]
            self._offset = dt.datetime(*parts, tzinfo=dt.timezone.utc) - dt.datetime.now(dt.timezone.utc)
        except Exception:  # noqa: BLE001 -- no clock reported: sign with ours and see
            pass

    def media2_url(self):
        """The Media2 service address, or None when the camera has no Media2 (then H.265 can't
        be discovered or selected over ONVIF at all)."""
        if self._media2_url is None:
            self._sync_clock()
            resp = self._call(self.device_url, "<tds:GetServices><tds:IncludeCapability>false"
                                               "</tds:IncludeCapability></tds:GetServices>")
            self._media2_url = ""
            for svc in resp.iterfind("tds:Service", NS):
                if svc.findtext("tds:Namespace", namespaces=NS) == MEDIA2_NS:
                    self._media2_url = svc.findtext("tds:XAddr", namespaces=NS) or ""
        return self._media2_url or None

    def _media2(self, body):
        url = self.media2_url()
        if not url:
            raise OnvifError("the camera has no ONVIF Media2 service")
        return self._call(url, body)

    # --- queries -------------------------------------------------------------------------

    def profiles(self):
        """[{token, name, encoderToken, streamUri}] for every profile with a video encoder."""
        resp = self._media2("<tr2:GetProfiles><tr2:Type>VideoEncoder</tr2:Type></tr2:GetProfiles>")
        out = []
        for p in resp.iterfind("tr2:Profiles", NS):
            ve = p.find("tr2:Configurations/tr2:VideoEncoder", NS)
            if ve is None:
                continue
            uri = self._media2(
                "<tr2:GetStreamUri><tr2:Protocol>RTSP</tr2:Protocol>"
                f"<tr2:ProfileToken>{escape(p.get('token'))}</tr2:ProfileToken></tr2:GetStreamUri>"
            ).findtext("tr2:Uri", namespaces=NS)
            out.append({"token": p.get("token"), "name": p.findtext("tr2:Name", namespaces=NS),
                        "encoderToken": ve.get("token"), "streamUri": uri})
        return out

    def encoder_config(self, token):
        """The encoder configuration as the camera reports it (an lxml element)."""
        resp = self._media2("<tr2:GetVideoEncoderConfigurations><tr2:ConfigurationToken>"
                            f"{escape(token)}</tr2:ConfigurationToken></tr2:GetVideoEncoderConfigurations>")
        cfg = resp.find("tr2:Configurations", NS)
        if cfg is None:
            raise OnvifError(f"no encoder configuration '{token}'")
        return cfg

    def encoder_options(self, token):
        """{"H264": {...}, "H265": {...}, ...}: what the encoder accepts, per encoding."""
        resp = self._media2("<tr2:GetVideoEncoderConfigurationOptions><tr2:ConfigurationToken>"
                            f"{escape(token)}</tr2:ConfigurationToken>"
                            "</tr2:GetVideoEncoderConfigurationOptions>")
        out = {}
        for o in resp.iterfind("tr2:Options", NS):
            enc = o.findtext("tt:Encoding", namespaces=NS)
            res = []
            for a in o.iterfind("tt:ResolutionsAvailable", NS):
                res.append({"width": int(a.findtext("tt:Width", namespaces=NS)),
                            "height": int(a.findtext("tt:Height", namespaces=NS)),
                            "frameRate": _range(a.find("tt:FrameRateRange", NS)),
                            "bitrate": _range(a.find("tt:BitrateRange", NS))})
            out[enc] = {
                "profiles": (o.get("ProfilesSupported") or "").split(),
                "govLength": [int(x) for x in (o.get("GovLengthRange") or "").split()],
                "frameRates": [float(x) for x in (o.get("FrameRatesSupported") or "").split()],
                "resolutions": res,
                "bitrate": _range(o.find("tt:BitrateRange", NS)),
            }
        return out

    def set_encoder_config(self, cfg):
        """SetVideoEncoderConfiguration with a full configuration element."""
        el = copy.deepcopy(cfg)
        el.tag = _q("tr2:Configuration")
        etree.cleanup_namespaces(el)
        self._media2("<tr2:SetVideoEncoderConfiguration>"
                     f"{etree.tostring(el).decode()}</tr2:SetVideoEncoderConfiguration>")


def for_camera(item):
    """A client for a `cameras` registry row."""
    return Media2(item["onvifHost"], item.get("onvifPort", 80), item["onvifUser"],
                  item["onvifPassword"])


def stream_profile(client, rtsp_url):
    """The profile whose stream is the one this system pulls -- the registered rtspUrl. The
    encoder behind it is the only one whose codec reaches MediaMTX; a camera has one per
    stream (cam-03: MAIN and SUB), and switching the other would change nothing we see."""
    profiles = client.profiles()
    for p in profiles:
        if same_stream(p["streamUri"], rtsp_url):
            return p
    raise OnvifError(f"none of the camera's {len(profiles)} profiles streams "
                     f"{urlsplit(rtsp_url or '').path or '(no rtspUrl)'} -- re-register the camera")


def _range(el):
    if el is None:
        return None
    return [float(el.findtext("tt:Min", namespaces=NS)), float(el.findtext("tt:Max", namespaces=NS))]


def summarize(cfg):
    """The parts of an encoder configuration a person needs to see."""
    def num(path):
        v = cfg.findtext(path, namespaces=NS)
        return float(v) if v else None
    return {"encoding": cfg.findtext("tt:Encoding", namespaces=NS), "profile": cfg.get("Profile"),
            "width": int(num("tt:Resolution/tt:Width") or 0),
            "height": int(num("tt:Resolution/tt:Height") or 0),
            "frameRate": num("tt:RateControl/tt:FrameRateLimit"),
            "bitrateKbps": num("tt:RateControl/tt:BitrateLimit"),
            "govLength": int(cfg.get("GovLength")) if cfg.get("GovLength") else None}


def same_stream(a, b):
    """Whether two RTSP URIs name the same stream. Path and query only: the registered URI may
    carry credentials, an explicit :554, or a different host spelling than the camera reports.
    Credentials in the query (cam-02's ?username=..&password=..) are ignored too."""
    def key(u):
        s = urlsplit(u or "")
        q = "&".join(sorted(p for p in s.query.split("&")
                            if p and not p.lower().startswith(("user", "pass", "pwd"))))
        return (s.path.rstrip("/") or "/", q)
    return key(a) == key(b)


def plan_config(cfg, opts, encoding, prefer_resolution=None):
    """A copy of `cfg` switched to `encoding`, every other field kept where the target
    encoding allows it and moved to the nearest allowed value where it doesn't.

    `prefer_resolution` is the (width, height) MediaMTX actually receives. It wins over the
    configuration's own when the target allows it, because the configuration can be stale
    (cam-03, module docstring): a codec switch should not also change the picture size.
    Pure function: no I/O, so the choices can be checked without a camera.
    """
    o = opts[encoding]
    new = copy.deepcopy(cfg)
    new.find("tt:Encoding", NS).text = encoding

    profiles = o["profiles"]
    if profiles:
        if new.get("Profile") not in profiles:
            pref = [p for p in PROFILE_PREFERENCE.get(encoding, []) if p in profiles]
            new.set("Profile", (pref or profiles)[0])
    elif "Profile" in new.attrib:
        del new.attrib["Profile"]

    cur = (int(cfg.findtext("tt:Resolution/tt:Width", namespaces=NS)),
           int(cfg.findtext("tt:Resolution/tt:Height", namespaces=NS)))
    allowed = {(r["width"], r["height"]): r for r in o["resolutions"]}
    if allowed:
        if prefer_resolution and tuple(prefer_resolution) in allowed:
            res = tuple(prefer_resolution)
        elif cur in allowed:
            res = cur
        else:
            # The largest size not above the current one, else the smallest there is.
            area = lambda r: r[0] * r[1]  # noqa: E731
            fits = [r for r in allowed if area(r) <= area(cur)]
            res = max(fits, key=area) if fits else min(allowed, key=area)
        new.find("tt:Resolution/tt:Width", NS).text = str(res[0])
        new.find("tt:Resolution/tt:Height", NS).text = str(res[1])
        per_res = allowed[res]
    else:
        per_res = {}

    if o["govLength"] and new.get("GovLength"):
        lo, hi = o["govLength"][0], o["govLength"][-1]
        new.set("GovLength", str(min(max(int(new.get("GovLength")), lo), hi)))

    rc = new.find("tt:RateControl", NS)
    if rc is not None:
        fr_el = rc.find("tt:FrameRateLimit", NS)
        if fr_el is not None and fr_el.text:
            fr = float(fr_el.text)
            if o["frameRates"] and fr not in o["frameRates"]:
                lower = [f for f in o["frameRates"] if f <= fr]
                fr = max(lower) if lower else min(o["frameRates"])
            if per_res.get("frameRate"):
                fr = min(max(fr, per_res["frameRate"][0]), per_res["frameRate"][1])
            fr_el.text = f"{fr:g}"
        br_el = rc.find("tt:BitrateLimit", NS)
        rng = per_res.get("bitrate") or o["bitrate"]
        if br_el is not None and br_el.text and rng:
            br_el.text = str(int(min(max(float(br_el.text), rng[0]), rng[1])))
    return new


def streams(client):
    """The camera's video streams, biggest picture first: [{"name": "main"|"sub"|"sub2"...,
    token, encoderToken, streamUri, encoding, width, height, fps, govLength, fpsChoices}].

    "main" is the largest picture rather than the first profile, because profile order and names
    are the vendor's (cam-02: MainStream/SubStream; cam-03: PROFILE_000/001). The sizes are the
    camera's claim, which can be stale (module docstring); a switch is proved on the wire.
    """
    out = []
    for p in client.profiles():
        s = summarize(client.encoder_config(p["encoderToken"]))
        opts = client.encoder_options(p["encoderToken"]).get(s["encoding"]) or {}
        out.append({**p, "encoding": s["encoding"], "width": s["width"], "height": s["height"],
                    "fps": s["frameRate"], "govLength": s["govLength"],
                    "fpsChoices": fps_choices(opts, (s["width"], s["height"]))})
    out.sort(key=lambda x: x["width"] * x["height"], reverse=True)
    for i, x in enumerate(out):
        x["name"] = "main" if i == 0 else ("sub" if i == 1 else f"sub{i}")
    return out


def fps_choices(opts, resolution=None):
    """The FPS_CHOICES this encoder accepts: in its supported list when it gives one, and inside
    the per-resolution frame-rate range when that is given too."""
    rates = opts.get("frameRates") or []
    rng = next((r["frameRate"] for r in opts.get("resolutions") or []
                if resolution and (r["width"], r["height"]) == tuple(resolution) and r["frameRate"]),
               None)
    return [f for f in FPS_CHOICES
            if (not rates or float(f) in rates) and (not rng or rng[0] <= f <= rng[1])]


def fps_config(cfg, opts, fps):
    """A copy of `cfg` at `fps`, with the keyframe interval kept in *seconds*: GovLength counts
    frames, so 100 frames at 20 fps (5 s) would become 10 s at 10 fps -- and a KVS fragment is
    one keyframe interval, so live HLS latency and clip granularity would double with it.
    Pure function, like plan_config."""
    new = copy.deepcopy(cfg)
    fr_el = new.find("tt:RateControl/tt:FrameRateLimit", NS)
    if fr_el is None:
        raise OnvifError("the encoder configuration has no frame-rate limit to set")
    old = float(fr_el.text or 0) or float(fps)
    fr_el.text = f"{fps:g}"
    if new.get("GovLength"):
        gov = max(1, round(int(new.get("GovLength")) * fps / old))
        if opts.get("govLength"):
            gov = min(max(gov, opts["govLength"][0]), opts["govLength"][-1])
        new.set("GovLength", str(gov))
    return new


def with_credentials(uri, registered):
    """`uri` (as the camera reports it) carrying the credentials of `registered`, the URI this
    system pulls today: userinfo, and user/password query parameters (cam-02's ?username=..&
    password=..). A camera's GetStreamUri usually omits them, and switching to another stream of
    the same camera must not lose them."""
    new, old = urlsplit(uri or ""), urlsplit(registered or "")
    netloc = new.netloc
    if "@" not in netloc and "@" in old.netloc:
        netloc = old.netloc.rsplit("@", 1)[0] + "@" + netloc
    have = {q.split("=", 1)[0].lower() for q in new.query.split("&") if q}
    extra = [q for q in old.query.split("&")
             if q and q.lower().startswith(("user", "pass", "pwd")) and q.split("=", 1)[0].lower() not in have]
    query = "&".join([q for q in new.query.split("&") if q] + extra)
    return urlunsplit((new.scheme, netloc, new.path, query, new.fragment))
