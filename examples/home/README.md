# The home side

What runs on the machine at home. It only ever **dials out** to the gateway: no port is
opened on the home router.

| | Over | Its address, as Traefik sees it |
| :-- | :-- | :-- |
| WireGuard client of trapdoor | UDP to `203.0.113.10:51820` | `10.8.10.10` |
| Tailscale node (Headscale) | HTTPS to `hs.example.com`, then direct or relayed | `100.64.0.10` |

Use both for failover, or either one alone.

## 1. WireGuard: a trapdoor client

In trapdoor, add a client named `home` with the address `10.8.10.10`. Set its **allowed IPs**
to `10.8.10.0/24` (only the VPN goes through the tunnel, not your home internet) and its
**keepalive** to 25 s (the tunnel stays open from the inside). Under *networks behind it*, add
`192.168.1.0/24` if Traefik should also reach other devices on the home LAN.

Download its config and install it on the home machine:

```sh
sudo apt install wireguard
sudo cp home.conf /etc/wireguard/wg-gateway.conf
sudo systemctl enable --now wg-quick@wg-gateway
sudo wg show wg-gateway        # "latest handshake: ... seconds ago"
```

For the home LAN behind it (only if you added `192.168.1.0/24` above), the home machine
forwards and masquerades into its LAN:

```sh
echo 'net.ipv4.ip_forward=1' | sudo tee /etc/sysctl.d/90-gateway.conf && sudo sysctl --system
sudo iptables -t nat -A POSTROUTING -s 10.8.10.0/24 -o eth0 -j MASQUERADE   # eth0: your LAN interface
```

(Make the rule permanent with `iptables-persistent`, or with `PostUp` / `PostDown` lines in
`wg-gateway.conf`.)

## 2. Tailscale: a node of your Headscale

```sh
docker compose -f compose.tailscale.yml up -d     # this folder; or install Tailscale natively
```

Then on the gateway:

```sh
docker compose exec headscale headscale nodes list     # the home node, and its 100.64 address
```

Give it a fixed address if you like (Headplane, *Machines*), and use that address in
`traefik/dynamic/routes.yml`.

## 3. Tell your apps who the proxy is

Requests arrive with the tunnel's address of the gateway as their source:
`10.8.10.1` over WireGuard (trapdoor's NAT), `100.64.x.x` over Tailscale. Apps that read
`X-Forwarded-For` must trust those ranges, or they answer **400 Bad Request**.

Home Assistant, `configuration.yaml`:

```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 10.8.10.0/24
    - 100.64.0.0/10
  ip_ban_enabled: true
  login_attempts_threshold: 5
```

Nextcloud, `config.php`: the same ranges under `trusted_proxies`, and the public name under
`trusted_domains`.
