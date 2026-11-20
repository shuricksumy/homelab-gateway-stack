#!/bin/sh
# trapdoor's own start script for wg0, run by its vpn container while wg0 is up
# (IFACE=wg0, SUBNET=the VPN range). NAT into the tunnel: Traefik's requests leave wg0 with
# trapdoor's address, so the home server answers through the tunnel. VPN clients among
# themselves keep their addresses (! -s "$SUBNET").
iptables -t nat -C POSTROUTING -o "$IFACE" ! -s "$SUBNET" -j MASQUERADE 2>/dev/null ||
	iptables -t nat -A POSTROUTING -o "$IFACE" ! -s "$SUBNET" -j MASQUERADE
