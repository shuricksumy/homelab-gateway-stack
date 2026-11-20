#!/bin/sh
# Routes from Traefik's network namespace into the tunnels. Traefik's entrypoint runs it every
# 30 s in the background (compose.core.yml), so routes come back by themselves after a restart.
# Traefik has NET_ADMIN for exactly this.
#
# ROUTES (Traefik's environment, from .env): space-separated "<subnet>=<gateway>" pairs; the
# gateway is the fixed edge-network address of the container that owns the tunnel.
for pair in $ROUTES; do
	net=${pair%%=*} via=${pair#*=}
	[ -n "$net" ] && [ -n "$via" ] && [ "$net" != "$pair" ] || continue
	ip route replace "$net" via "$via" || echo "routes.sh: $net via $via failed"
done
