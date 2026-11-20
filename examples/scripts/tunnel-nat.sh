#!/bin/sh
# NAT into a tunnel: requests from the edge network (Traefik) leave the tunnel interface with
# the tunnel's own address, so the far end (your home server) knows how to answer. Without it
# the home server would answer 172.30.0.x, which it has no route to: everything times out
# although the tunnel is up.
#
# Runs next to a tunnel container (network_mode: service:<it>, NET_ADMIN) and checks the rule
# every 60 s, so it comes back after the tunnel container restarts.
#   OUT_IFACE  the tunnel interface (tailscale0, zt+)
#   FROM       the edge network (EDGE_SUBNET)
set -u
apk add --no-cache iptables >/dev/null 2>&1 || { echo "cannot install iptables"; exit 1; }
while true; do
	iptables -t nat -C POSTROUTING -s "$FROM" -o "$OUT_IFACE" -j MASQUERADE 2>/dev/null ||
		iptables -t nat -A POSTROUTING -s "$FROM" -o "$OUT_IFACE" -j MASQUERADE
	sleep 60
done
