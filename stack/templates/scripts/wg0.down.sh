#!/bin/sh
# Undoes wg0.up.sh when wg0 stops.
iptables -t nat -D POSTROUTING -o "$IFACE" ! -s "$SUBNET" -j MASQUERADE 2>/dev/null || true
