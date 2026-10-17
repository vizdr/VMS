"""WS-Discovery scan + ONVIF enrichment, shared by discover-onvif.py (CLI) and the local
ONVIF admin GUI (adapter/onvif-admin/app.py). See discover-onvif.py's module docstring
for how the protocol itself works -- this module is just the two stages factored out so
they're not duplicated between the CLI tool and the web app.
"""
import asyncio
import os
import sys

from onvif import ONVIFCamera

import onvif_media2
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
    return {"encoding": a.Encoding, "sampleRateKHz": a.SampleRate, "bitrateKbps": a.Bitrate,
            "supported": a.Encoding in AUDIO_SUPPORTED}


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

    await cam.close()
    return {
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
    by_profile = details.pop("audio_by_profile")
    try:
        found = onvif_media2.streams(onvif_media2.Media2(details["host"], details["port"],
                                                         user, password))
    except Exception as e:  # noqa: BLE001 -- the rest of the card is still worth showing
        details["streams"], details["streamsError"] = [], f"{type(e).__name__}: {e}"
        return details
    details["streams"] = [
        {"name": x["name"], "uri": x["streamUri"], "encoding": x["encoding"],
         "width": x["width"], "height": x["height"], "fps": x["fps"],
         "fpsChoices": x["fpsChoices"], "audio": by_profile.get(x["token"], details["audio"])}
        for x in found]
    return details
