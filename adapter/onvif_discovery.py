"""WS-Discovery scan + ONVIF enrichment, shared by discover-onvif.py (CLI) and the local
ONVIF admin GUI (adapter/onvif-admin/app.py). See discover-onvif.py's module docstring
for how the protocol itself works -- this module is just the two stages factored out so
they're not duplicated between the CLI tool and the web app.
"""
import asyncio
import base64
import hashlib
import os
import re
import socket
import sys
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from onvif import ONVIFCamera

import onvif_events
import onvif_media2
import requests
from wsdiscovery.discovery import ThreadedWSDiscovery as WSDiscovery
from wsdiscovery import QName

# $VMS_HOME if set, otherwise the repo root (this file lives in adapter/).
VMS_HOME = os.environ.get("VMS_HOME") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WSDL_DIR = os.path.join(VMS_HOME, "venv-adapter", "lib",
    f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages", "onvif", "wsdl")
NVT_TYPE = QName("http://www.onvif.org/ver10/network/wsdl", "NetworkVideoTransmitter")
# Audio encodings the producers can take (stream-channel.sh: G.711 of either law -> AAC). Media1
# names G711, G726 and AAC; a camera with another one is shown, but not registered audio-capable,
# because audioCapable lets the user switch audio on, and a producer waiting on an audio pad it
# can't decode stalls the video too (guide §18).
AUDIO_SUPPORTED = {"G711"}


def scan(timeout: int = 4):
    """Anonymous multicast probe. Returns wsdiscovery Service objects -- XAddrs + Scopes
    only, no stream URL and no auth, since discovery itself carries no credentials."""
    wsd = WSDiscovery()
    wsd.start()
    try:
        return wsd.searchServices(types=[NVT_TYPE], timeout=timeout)
    finally:
        wsd.stop()


def _audio(profile) -> dict | None:
    """The microphone a Media1 profile streams, from its audio encoder, or None. ONVIF can't
    say A-law or mu-law ("G711" covers both); the producer reads that from the wire."""
    a = getattr(profile, "AudioEncoderConfiguration", None)
    if not a or not a.Encoding:
        return None
    return {"encoding": a.Encoding, "sampleRateKHz": _per_kilo(a.SampleRate),
            "bitrateKbps": _per_kilo(a.Bitrate), "supported": a.Encoding in AUDIO_SUPPORTED}


def _per_kilo(value):
    """A Media1 audio figure in the spec's unit (kHz, kbit/s). Some cameras send Hz and bit/s:
    cam-02 reports 8000 and 64000 where cam-03 reports 8 and 64. No audio rate is a thousand kHz
    or a thousand kbit/s, so anything that large is in the base unit."""
    if value is None:
        return None
    return value / 1000 if value >= 1000 else value


# SDP encoding names -> the Media1 names _audio() reports, so both sources read the same.
SDP_AUDIO = {"PCMA": "G711", "PCMU": "G711", "MPEG4-GENERIC": "AAC", "MP4A-LATM": "AAC",
             "G726-16": "G726", "G726-24": "G726", "G726-32": "G726", "G726-40": "G726"}
SDP_STATIC = {"0": ("PCMU", 8000), "8": ("PCMA", 8000)}   # RFC 3551 static payload types


class RtspUnauthorized(OSError):
    """The camera's RTSP answered 401 to the credentials sent, or to none."""


def _describe(uri: str, user: str | None, password: str, timeout: float = 3) -> str:
    """The stream's SDP, by one RTSP DESCRIBE (Basic or Digest auth on a 401). No SETUP, so the
    camera opens no session and sends no media. Credentials in the URI win over user/password;
    user None asks without any."""
    u = urlsplit(uri)
    if u.username is not None:
        user, password = unquote(u.username), unquote(u.password or "")
    bare = urlunsplit((u.scheme, u.hostname + (f":{u.port}" if u.port else ""), u.path, u.query, ""))
    auth = None
    for cseq in (1, 2):
        req = f"DESCRIBE {bare} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\n"
        req += f"Authorization: {auth}\r\n\r\n" if auth else "\r\n"
        with socket.create_connection((u.hostname, u.port or 554), timeout=timeout) as s:
            s.sendall(req.encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            head = head.decode(errors="replace")
            m = re.search(r"^Content-Length:\s*(\d+)", head, re.I | re.M)
            while m and len(body) < int(m.group(1)):
                chunk = s.recv(4096)
                if not chunk:
                    break
                body += chunk
        status = head.split(" ", 2)[1] if head.count(" ") >= 2 else ""
        if status == "200":
            return body.decode(errors="replace")
        challenge = re.search(r"^WWW-Authenticate:\s*(.+)$", head, re.I | re.M)
        if status == "401" and (auth or user is None):
            raise RtspUnauthorized("RTSP DESCRIBE answered 401")
        if status != "401" or not challenge:
            raise OSError(f"RTSP DESCRIBE answered {status or 'nothing'}")
        auth = _rtsp_auth(challenge.group(1).strip(), user, password, bare)
    raise OSError("RTSP DESCRIBE refused the credentials")


def _rtsp_auth(challenge: str, user: str, password: str, uri: str) -> str:
    if challenge.lower().startswith("basic"):
        return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    p = dict(re.findall(r'(\w+)="?([^",]*)"?', challenge))
    md5 = lambda x: hashlib.md5(x.encode()).hexdigest()   # noqa: E731
    ha1, ha2 = md5(f"{user}:{p.get('realm', '')}:{password}"), md5(f"DESCRIBE:{uri}")
    resp = md5(f"{ha1}:{p.get('nonce', '')}:{ha2}")
    return (f'Digest username="{user}", realm="{p.get("realm", "")}", nonce="{p.get("nonce", "")}", '
            f'uri="{uri}", response="{resp}"')


# MediaMTX refuses a source URL with a username and an empty password ("username and password
# must be both provided"), so a camera without one is pulled with this in its place. It works
# only where the camera ignores the password when none is set (#66), which rtsp_login checks.
RTSP_PLACEHOLDER = "x"


def _has_login(uri: str) -> bool:
    u = urlsplit(uri)
    return u.username is not None or any(
        q.split("=", 1)[0].lower().startswith(("user", "pass", "pwd")) for q in u.query.split("&") if q)


def rtsp_login(uri: str, user: str, password: str) -> str:
    """`uri` as MediaMTX must pull it. A camera's GetStreamUri leaves out the login its RTSP may
    demand; registered without it, every pull is a 401 and the camera never streams (#66). So: a
    URI that carries a login, or a stream that plays without one, is returned as given; a 401
    gets the ONVIF login in the URI, checked by one DESCRIBE. A camera that can't be reached is
    left as given (registration never depended on that). Raises RtspUnauthorized when the
    camera refuses the login, with what to do in the message."""
    if not uri or _has_login(uri):
        return uri
    try:
        _describe(uri, None, "")
        return uri
    except RtspUnauthorized:
        pass
    except OSError:
        return uri
    pw = password or RTSP_PLACEHOLDER
    try:
        _describe(uri, user, pw)
    except RtspUnauthorized:
        raise RtspUnauthorized(
            f"the camera's RTSP refuses user '{user}' with " +
            ("this password" if password else
             "an empty password (MediaMTX can't send an empty one: set a password on the camera)") +
            "; or put a working login in the stream URI (rtsp://user:password@...)") from None
    u = urlsplit(uri)
    return urlunsplit((u.scheme, f"{quote(user, safe='')}:{quote(pw, safe='')}@{u.netloc}",
                       u.path, u.query, u.fragment))


def stream_audio(uri: str, user: str, password: str) -> dict | None:
    """The audio track a stream actually carries, from its SDP, in _audio()'s shape; None when it
    has none. For cameras whose ONVIF profile doesn't list the microphone they stream (a 5831-F-Q
    with "audio&video" set in its web page: G.711 on the wire, no AudioEncoderConfiguration)."""
    sdp = _describe(uri, user, password)
    for media in re.split(r"^(?=m=)", sdp, flags=re.M):
        if not media.startswith("m=audio"):
            continue
        pts = media.split("\n", 1)[0].split()[3:]
        maps = {pt: (name, int(rate)) for pt, name, rate in
                re.findall(r"^a=rtpmap:(\d+)\s+([^/\s]+)/(\d+)", media, re.M)}
        for pt in pts:
            name, rate = maps.get(pt) or SDP_STATIC.get(pt, (None, None))
            if name:
                enc = SDP_AUDIO.get(name.upper(), name.upper())
                return {"encoding": enc, "sampleRateKHz": rate / 1000, "bitrateKbps": None,
                        "supported": enc in AUDIO_SUPPORTED, "fromStream": True}
    return None


async def enrich(xaddr: str, user: str, password: str) -> dict:
    """Authenticated second stage: turns a bare XAddrs into device info + a real RTSP
    stream URI, and reports whether the device exposes IR-cut control and a microphone (so the
    caller can set hasIrControl and audioCapable without a second round trip)."""
    host = xaddr.split("://")[1].split("/")[0].split(":")[0]
    addr_port = xaddr.split("://")[1].split("/")[0]
    port = int(addr_port.split(":")[1]) if ":" in addr_port else 80

    cam = ONVIFCamera(host, port, user, password, wsdl_dir=WSDL_DIR)
    await cam.update_xaddrs()

    devicemgmt = await cam.create_devicemgmt_service()
    info = await devicemgmt.GetDeviceInformation()

    media = await cam.create_media_service()
    profiles = await media.GetProfiles()
    stream_uri = None
    video_source_token = None
    if profiles:
        video_source_token = profiles[0].VideoSourceConfiguration.SourceToken
        uri_resp = await media.GetStreamUri({
            "StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
            "ProfileToken": profiles[0].token,
        })
        stream_uri = uri_resp.Uri

    has_ir_control = False
    if video_source_token:
        try:
            imaging = await cam.create_imaging_service()
            options = await imaging.GetOptions({"VideoSourceToken": video_source_token})
            has_ir_control = bool(options.IrCutFilterModes)
        except Exception:
            has_ir_control = False

    # The recording triggers it can serve, from its own event list (onvif_events.detection_map):
    # {mode: {topic, field, match}}. What it offers, not proof it fires (FoundAndFixed.md #64).
    try:
        detection, topics = await onvif_events.camera_detection(cam)
        detection_error = None
    except Exception as e:  # noqa: BLE001 -- an event service is optional; the card still shows
        detection, topics, detection_error = {}, set(), f"{type(e).__name__}: {e}"

    try:
        ports = await http_ports(cam)
    except Exception:  # noqa: BLE001 -- the web link is a convenience; try port 80 alone
        ports = []

    await cam.close()
    return {
        "httpPorts": ports,
        "detection": detection,
        "eventTopics": sorted(topics),
        "detectionError": detection_error,
        "host": host,
        "port": port,
        "manufacturer": info.Manufacturer,
        "model": info.Model,
        "firmware": info.FirmwareVersion,
        "profile": profiles[0].Name if profiles else None,
        "stream_uri": stream_uri,
        "has_ir_control": has_ir_control,
        "audio": _audio(profiles[0]) if profiles else None,
        "audio_by_profile": {p.token: _audio(p) for p in profiles},
    }


def enrich_sync(xaddr: str, user: str, password: str) -> dict:
    """enrich(), plus the camera's streams over Media2 (onvif_media2.streams: size, codec, frame
    rate and the frame rates it accepts), each with its microphone -- what the registration form
    offers. A camera without Media2 still enriches, with an empty stream list."""
    details = asyncio.run(enrich(xaddr, user, password))
    details["webUrl"] = web_url(details["host"], details.pop("httpPorts"))
    by_profile = details.pop("audio_by_profile")
    if details["audio"] is None and details["stream_uri"]:
        details["audio"] = _wire_audio(details["stream_uri"], user, password)
    try:
        found = onvif_media2.streams(onvif_media2.Media2(details["host"], details["port"],
                                                         user, password))
    except Exception as e:  # noqa: BLE001 -- the rest of the card is still worth showing
        details["streams"], details["streamsError"] = [], f"{type(e).__name__}: {e}"
        return details
    details["streams"] = [
        {"name": x["name"], "uri": x["streamUri"], "encoding": x["encoding"],
         "width": x["width"], "height": x["height"], "fps": x["fps"],
         "fpsChoices": x["fpsChoices"],
         "audio": by_profile.get(x["token"]) or _wire_audio(x["streamUri"], user, password)}
        for x in found]
    return details


def _wire_audio(uri, user, password):
    """stream_audio(), or None when the stream can't be asked: ONVIF's answer then stands."""
    try:
        return stream_audio(uri, user, password)
    except (OSError, ValueError, IndexError):
        return None


async def http_ports(cam) -> list[int]:
    """The HTTP ports the camera reports enabled (GetNetworkProtocols), for its web interface."""
    dm = await cam.create_devicemgmt_service()
    ports = []
    for proto in await dm.GetNetworkProtocols() or []:
        if proto.Name == "HTTP" and proto.Enabled:
            ports += [int(p) for p in (proto.Port or [])]
    return ports


def web_url(host: str, ports=None) -> str | None:
    """The camera's own web interface, or None when it has none we can see.

    Asked of the camera, not assumed: some cameras serve only ONVIF on HTTP and have no settings
    page (a Tapo, for one), and a link to nothing would be worse than none. A page is a 200 with
    HTML, or a 401/403 asking for a login; the ports come from GetNetworkProtocols, then 80. The
    link is for a browser on the camera's LAN -- from anywhere else it leads nowhere.
    """
    seen = []
    for port in list(ports or []) + [80]:
        if port in seen:
            continue
        seen.append(port)
        url = f"http://{host}/" if port == 80 else f"http://{host}:{port}/"
        try:
            r = requests.get(url, timeout=3, allow_redirects=True)
        except requests.RequestException:
            continue
        ctype = r.headers.get("Content-Type", "").lower()
        if (r.status_code == 200 and "html" in ctype) or \
                (r.status_code in (401, 403) and ("html" in ctype or "WWW-Authenticate" in r.headers)):
            return url
    return None

