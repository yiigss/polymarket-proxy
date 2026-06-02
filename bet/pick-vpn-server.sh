#!/usr/bin/env bash
# pick-vpn-server.sh — select the lowest-load live NordVPN Switzerland (CH) server
# and overwrite vpn/ch.ovpn before OpenVPN connects.
# Prevents runner death from retired NordVPN servers.
set -euo pipefail

echo "[VPN] Fetching best NordVPN Switzerland server..."

RESP=$(curl -sf --max-time 10 \
  "https://api.nordvpn.com/v1/servers/recommendations?filters%5Bcountry_id%5D=209&filters%5Bservers_technologies%5D%5Bidentifier%5D=openvpn_tcp&limit=3")

HOST=$(echo "$RESP" | python3 -c "
import sys, json
servers = json.load(sys.stdin)
s = servers[0]
print(s['hostname'])
")

echo "[VPN] Selected: $HOST"

CONFIG_URL="https://downloads.nordcdn.com/configs/files/ovpn_tcp/servers/${HOST}.tcp.ovpn"
echo "[VPN] Downloading: $CONFIG_URL"
curl -sf --max-time 15 "$CONFIG_URL" -o vpn/ch.ovpn

echo "[VPN] Config ready — $(grep 'verify-x509-name' vpn/ch.ovpn)"
