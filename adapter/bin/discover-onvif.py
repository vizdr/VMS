#!/usr/bin/env python3
"""ONVIF WS-Discovery scan of the local network -- lists NetworkVideoTransmitter
devices (IP, service address, scopes) and, if credentials are given, enriches each
match with its manufacturer/model and RTSP stream URI via the ONVIF Device/Media
services. Discovery alone only proves a device exists and where its ONVIF service
lives (XAddrs) -- getting a usable stream URL always needs an authenticated
Media.GetStreamUri call, which is why this is a two-stage process.

The actual scan/enrich logic lives in adapter/onvif_discovery.py, shared with the local
admin GUI (adapter/onvif-admin/app.py) -- this file is just the CLI wrapper.

Usage:
  python3 discover-onvif.py                              # scan only
  python3 discover-onvif.py --user admin --password ***  # scan + enrich
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import onvif_discovery


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--timeout", type=int, default=4, help="WS-Discovery probe timeout in seconds (default 4)")
    ap.add_argument("--user", help="ONVIF username -- enables the enrichment stage (device info + stream URI)")
    ap.add_argument("--password", help="ONVIF password -- required if --user is given")
    args = ap.parse_args()

    print(f"Probing 239.255.255.250:3702 for NetworkVideoTransmitter devices ({args.timeout}s)...")
    services = onvif_discovery.scan(args.timeout)

    if not services:
        print("No ONVIF devices responded. Common causes: device on a different VLAN/subnet "
              "(WS-Discovery's multicast doesn't cross routers by design), the camera's "
              "discovery is disabled in its own web UI, or it needs longer than "
              f"--timeout {args.timeout}.")
        return

    print(f"\nFound {len(services)} device(s):\n")
    for svc in services:
        xaddrs = svc.getXAddrs()
        scopes = [s.getValue() if hasattr(s, "getValue") else str(s) for s in svc.getScopes()]
        print(f"  XAddrs:  {', '.join(xaddrs)}")
        print(f"  Scopes:  {scopes}")

        if args.user and xaddrs:
            try:
                details = onvif_discovery.enrich_sync(xaddrs[0], args.user, args.password)
                print(f"  Device:  {details['manufacturer']} {details['model']} (fw {details['firmware']})")
                print(f"  Profile: {details['profile']}")
                print(f"  Stream:  {details['stream_uri']}")
                print(f"  IR ctl:  {details['has_ir_control']}")
            except Exception as e:
                print(f"  Enrichment failed: {e}")
        print()


if __name__ == "__main__":
    main()
