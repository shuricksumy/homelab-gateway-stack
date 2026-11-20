#!/bin/sh
# Routes from Traefik's network namespace into the tunnels. Runs in the traefik-routes
# container (network_mode: service:traefik, NET_ADMIN), every 30 s, so a restarted Traefik
# gets them back within half a minute.
#
# ROUTES: space-separated "<subnet>=<gateway>" pairs; the gateway is the fixed address, on the
# edge network, of the container that owns that tunnel (compose.yml, .env). Pairs with an
# empty side are skipped, so unused tunnels can stay in the list.
set -u
while true; do
	for pair in $ROUTES; do
		net=${pair%%=*} via=${pair#*=}
		[ -n "$net" ] && [ -n "$via" ] && [ "$net" != "$pair" ] || continue
		# replace: idempotent, and fixes a route whose gateway changed
		ip route replace "$net" via "$via" 2>/dev/null || echo "route $net via $via: failed (is the gateway on the edge network?)"
	done
	sleep 30
done
