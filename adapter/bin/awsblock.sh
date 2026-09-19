#!/usr/bin/env bash
# Simulate a WAN outage to AWS only. Both address families -- §10.2's IPv4-only snippet
# is silently ineffective on a dual-stack LAN. Anthropic (160.79.x / 2607:6bc0::) is
# outside every range below, so a developer session survives.
V4="3.0.0.0/8 18.0.0.0/8 35.0.0.0/8 52.0.0.0/8 54.0.0.0/8 13.32.0.0/12 99.80.0.0/12"
V6="2a05::/16 2600:1f00::/24 2a01:578::/32"
case "$1" in
  on)
    for n in $V4; do sudo iptables  -A OUTPUT -d $n -j DROP; done
    for n in $V6; do sudo ip6tables -A OUTPUT -d $n -j DROP; done ;;
  off)
    for n in $V4; do while sudo iptables  -D OUTPUT -d $n -j DROP 2>/dev/null; do :; done; done
    for n in $V6; do while sudo ip6tables -D OUTPUT -d $n -j DROP 2>/dev/null; do :; done; done ;;
esac
echo "v4 DROP=$(sudo iptables -L OUTPUT -n | grep -c DROP)  v6 DROP=$(sudo ip6tables -L OUTPUT -n | grep -c DROP)"
for h in kinesisvideo.eu-central-1.amazonaws.com a3dp4umq4qv6ul-ats.iot.eu-central-1.amazonaws.com; do
  printf "  %-50s " "$h"
  timeout 6 curl -s -o /dev/null -w "reachable (%{http_code})\n" "https://$h" 2>/dev/null || echo "UNREACHABLE"
done
printf "  %-50s " "api.anthropic.com (must stay reachable)"
timeout 6 curl -s -o /dev/null -w "reachable (%{http_code})\n" https://api.anthropic.com 2>/dev/null || echo "*** CUT ***"
