#!/bin/sh
# trapdoor own start script for the WireGuard server wg0: copy to data/trapdoor/wireguard/.
# The vpn container runs it while wg0 is up, with IFACE=wg0 and SUBNET=10.8.10.0/24.
#
# NAT into the tunnel: Traefik's requests (from the edge network) leave wg0 with this server's
# tunnel address, so the home server answers through the tunnel. Your own VPN clients are
# not affected (! -s "$SUBNET"): between them, addresses stay as they are.
iptables -t nat -A POSTROUTING -o "$IFACE" ! -s "$SUBNET" -j MASQUERADE
