#!/usr/bin/env bash
# pick-vpn-server.sh — fetch top 3 NordVPN Switzerland servers and download
# their OpenVPN configs into /tmp/vpn-server-{1,2,3}.ovpn for the retry loop.
set -euo pipefail

echo "[VPN] Fetching best NordVPN Switzerland servers..."

RESP=$(curl -sf --max-time 10 \
  "https://api.nordvpn.com/v1/servers/recommendations?filters%5Bcountry_id%5D=209&filters%5Bservers_technologies%5D%5Bidentifier%5D=openvpn_tcp&limit=5")

HOSTS=$(echo "$RESP" | python3 -c "
import sys, json
servers = json.load(sys.stdin)
for s in servers[:3]:
    print(s['hostname'])
")

echo "[VPN] Candidates: $(echo "$HOSTS" | tr '\n' ' ')"

i=1
while IFS= read -r HOST; do
    CONFIG_URL="https://downloads.nordcdn.com/configs/files/ovpn_tcp/servers/${HOST}.tcp.ovpn"
    echo "[VPN] Downloading config $i: $HOST"
    if curl -sf --max-time 15 "$CONFIG_URL" -o "/tmp/vpn-server-${i}.ovpn"; then
        echo "[VPN] Config $i ready — $(grep 'verify-x509-name' /tmp/vpn-server-${i}.ovpn)"
    else
        echo "[VPN] WARNING: Could not download config for $HOST"
    fi
    i=$((i+1))
done <<< "$HOSTS"

# Copy primary as vpn/ch.ovpn for compatibility
cp /tmp/vpn-server-1.ovpn vpn/ch.ovpn
echo "[VPN] Primary set to $(grep 'verify-x509-name' vpn/ch.ovpn)"
