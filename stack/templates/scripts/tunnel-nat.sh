#!/bin/sh
# NAT into a tunnel, then the container's own program:
#   tunnel-nat.sh <the image's entrypoint and arguments>
# Requests from the edge network (Traefik) leave the tunnel interface with the tunnel's own
# address, so the far end (your home server) can answer them. The rule is checked every 30 s,
# so it is there as soon as the tunnel interface exists, and again after any restart.
#   NAT_FROM   the edge network          NAT_IFACE  the tunnel interface (tailscale0, zt+)
(
	while :; do
		iptables -t nat -C POSTROUTING -s "$NAT_FROM" -o "$NAT_IFACE" -j MASQUERADE 2>/dev/null ||
			iptables -t nat -A POSTROUTING -s "$NAT_FROM" -o "$NAT_IFACE" -j MASQUERADE
		sleep 30
	done
) &
exec "$@"
