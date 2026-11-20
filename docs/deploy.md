# Deploy, step by step

From an empty cloud server to the whole stack. **Each phase ends with a check: do not start
the next phase before it passes.** Phases 6 to 9 are optional and can come later.

| Phase | Gives you |
| :-- | :-- |
| [0. Before you start](#0-before-you-start) | a server, DNS, the files |
| [1. The core](#1-the-core-traefik-and-authelia) | Traefik with certificates, the login portal, the configurator |
| [2. Your VPN](#2-your-vpn-trapdoor) | trapdoor, a WireGuard server, your phone connected |
| [3. DNS for VPN clients](#3-dns-for-vpn-clients-adguard-home) | the admin pages by name, over the VPN only |
| [4. Home over WireGuard](#4-home-over-wireguard) | your first home service on the internet |
| [5. The second tunnel](#5-the-second-tunnel-headscale-and-tailscale) | Tailscale first, WireGuard as fallback |
| [6. CrowdSec](#6-crowdsec) | automatic bans |
| [7. Traefik Manager](#7-traefik-manager) | routes edited in a browser |
| [8. The start page](#8-the-start-page-foyer) | foyer with every admin page in it |
| [9. ZeroTier](#9-zerotier-optional) | an optional third path |

Commands run in the stack folder on the server (`/opt/gateway` here, a copy of `examples/`).

## 0. Before you start

**The server.** Any cloud VM with a public IPv4 address and 2 GB RAM (1 GB without ZeroTier),
Debian or Ubuntu, Docker Engine with the Compose plugin **2.33 or newer**
(`docker compose version`; older versions ignore `gw_priority`, see phase 2).

**The cloud firewall** (security group): allow in TCP 22 (SSH, ideally only from your
address), TCP 80, TCP 443, UDP 51820. Later: UDP 1194 for OpenVPN, UDP 9993 for ZeroTier.
Nothing else: the admin pages need no port.

**DNS.** Public `A` records pointing at the server, **only for the public names**:

| Name | Public record | Why |
| :-- | :-- | :-- |
| `auth.example.com` | `A 203.0.113.10` | the login portal |
| `hs.example.com` | `A 203.0.113.10` | Headscale (phase 5) |
| `home.example.com`, `photos.example.com`, ... | `A 203.0.113.10` | your public services |
| `vpn`, `authcfg`, `tm`, `dns`, `start`, `zt`, `router` | **none** | admin pages: resolved by AdGuard for VPN clients only |

Certificates for the admin names come from a **DNS challenge** (they are not reachable from the
internet). Create an API token for your DNS provider (Cloudflare: *Zone · DNS · Edit* for the
zone) and put it in `.env` as `CF_DNS_API_TOKEN`. Another provider: change `provider:` in
`traefik/traefik.yml` and the variable in `compose.yml` (see Traefik's ACME provider list).

**The files.**

```sh
sudo mkdir -p /opt/gateway && sudo chown "$USER" /opt/gateway
git clone https://github.com/shuricksumy/homelab-gateway-stack /tmp/hgs
cp -r /tmp/hgs/examples/. /opt/gateway/ && cd /opt/gateway
cp .env.example .env && chmod 600 .env
```

Replace `example.com` everywhere (`grep -rl example.com . | xargs sed -i 's/example\.com/yourdomain.tld/g'`),
then fill in `.env`. If `172.30.0.0/24` is taken on the server (`docker network ls`,
`ip route`), pick another range and change it in `.env` **and** in `traefik/dynamic/core.yml`,
`traefik/optional/*.yml` and `authelia/managed/access_control.example.yml`.

## 1. The core: Traefik and Authelia

Authelia, Redis, the configurator and its socket proxy are the production example of
[authelia-webui](https://github.com/shuricksumy/authelia-webui/tree/main/examples/production).
Follow **its README, sections 1 to 3**, in this folder: it shows how to create
`authelia/configuration.yml` (from `configuration.example.yml` here), the files in
`authelia/managed/` (from the `*.example.yml` here), every secret, and the ownership.

```sh
sudo chown -R 1000:1000 authelia && mkdir -p authelia/logs data/redis data/letsencrypt data/logs/traefik
sudo chown -R 1000:1000 data/redis
docker compose config -q && docker compose up -d
docker compose ps                                  # all running; socket-proxy healthy
docker compose logs traefik | grep -i error        # nothing but, at most, a certificate retry
```

**Check:**
- `https://auth.example.com` shows the Authelia login, with a valid certificate.
- `docker compose exec traefik ip route` lists the tunnel routes (`10.8.10.0/24 via 172.30.0.30`, ...).
  They point at nothing yet; that is fine.

The configurator is VPN-only, so its first login waits for phase 3.

## 2. Your VPN: trapdoor

```sh
sed -i 's/^COMPOSE_FILE=.*/COMPOSE_FILE=compose.yml:compose.trapdoor.yml/' .env
mkdir -p data/trapdoor/{openvpn,wireguard,amneziawg,web} && sudo chown 10001:10001 data/trapdoor/web
docker compose up -d
docker compose exec -it trapdoor-web trapdoor user add admin
```

Open the web UI through an SSH tunnel (it is published on the server's loopback only):
`ssh -L 8080:127.0.0.1:8080 you@203.0.113.10`, then `http://localhost:8080` on your computer.

1. **Add a server**: WireGuard, interface `wg0`, network `10.8.10.0/24`, port `51820`,
   public address `203.0.113.10` (or a name pointing at it).
2. **Scripts and extra rules** (Server page): the NAT into the tunnel that phase 4 needs.
   Copy the two scripts and they run while `wg0` is up:
   ```sh
   cp trapdoor/wg0.up.sh trapdoor/wg0.down.sh data/trapdoor/wireguard/
   ```
3. **Your phone** (Clients, *Add*): DNS `172.30.0.53`; allowed IPs `10.8.10.0/24, 172.30.0.0/24`
   (only the VPN and the admin pages go through the tunnel) or `0.0.0.0/0` (everything).
   Scan the QR code in the WireGuard app.

**Check:**

```sh
docker compose exec trapdoor-vpn ip route | head -1     # default via the default network, not edge
docker compose exec trapdoor-vpn wg show wg0            # the phone: latest handshake seconds ago
docker compose exec trapdoor-vpn iptables -t nat -S POSTROUTING
#  -s 10.8.10.0/24 -o eth0 -j MASQUERADE        internet for VPN clients (eth0 = default network)
#  ! -s 10.8.10.0/24 -o wg0 -j MASQUERADE       NAT into the tunnel (wg0.up.sh)
```

If the default route goes through the edge network, your Compose ignored `gw_priority`: update
Docker, or set `VPN_NAT_IFACE` in `compose.trapdoor.yml` to the default network's interface.

## 3. DNS for VPN clients: AdGuard Home

On the phone, with the VPN on, open `http://172.30.0.53:3000` (AdGuard's first-run wizard;
VPN clients reach the edge network directly). Admin interface on port **3000**, DNS on port
**53**, create its login.

Then, in AdGuard Home:

1. **Filters → DNS rewrites**: every admin name → Traefik's internal address:
   `vpn.example.com`, `authcfg.example.com`, `tm.example.com`, `dns.example.com`,
   `start.example.com`, `zt.example.com`, `router.example.com`, `hs.example.com`
   → `172.30.0.2`. (One wildcard `*.example.com` → `172.30.0.2` also works: then the public
   names stay inside the tunnel too, which is fine for VPN clients.)
2. **Settings → DNS settings**: upstream servers of your choice (e.g. `https://dns.quad9.net/dns-query`).

In trapdoor, **Server → Scripts and extra rules → Send the clients' DNS to `172.30.0.53`**: VPN
clients that ignore the pushed DNS get redirected there too.

**Check** (phone on the VPN):
- `vpn.example.com` resolves to `172.30.0.2`; on mobile data **without** the VPN it does not resolve.
- `https://authcfg.example.com:8443` asks for the Authelia login. Sign in as the first admin (the
  2FA registration code is in `authelia/notification.txt` until SMTP is set up). Then the
  configurator opens. Add your real users there; give yourself the `admin` group.
- `https://vpn.example.com:8443`: Authelia, then trapdoor's own login. From now on manage trapdoor
  here; the SSH tunnel is not needed any more.
- **Off the VPN**, `https://<server-ip>:8443` times out. The internal entry point is not on the
  public address.

## 4. Home over WireGuard

Follow [`home/README.md`](../examples/home/README.md), part 1, on the home server: a trapdoor
client `home` at `10.8.10.10`, allowed IPs `10.8.10.0/24`, keepalive 25 s.

Until phase 5, point the services in `traefik/dynamic/routes.yml` at WireGuard directly: in each
router, `service: home-wg` instead of `service: home`. Traefik reloads the file by itself.

Trust the tunnel in your apps (`home/README.md`, part 3), or they answer 400.

**Check:**

```sh
docker compose exec trapdoor-vpn wg show wg0 | grep -A3 <home-public-key>    # handshake
docker compose exec traefik wget -qO- -T 5 http://10.8.10.10:8123/manifest.json | head -c 80
```

Then `https://home.example.com` from anywhere: your Home Assistant.

## 5. The second tunnel: Headscale and Tailscale

```sh
sed -i 's/^COMPOSE_FILE=.*/&:compose.headscale.yml/' .env
cp traefik/optional/headscale.yml traefik/dynamic/
mkdir -p data/headscale data/headplane data/tailscale
# headplane/config.yaml: replace the cookie_secret (openssl rand -hex 16)
docker compose up -d headscale headplane
docker compose exec headscale headscale users create gateway
docker compose exec headscale headscale users list                     # the user's ID
docker compose exec headscale headscale preauthkeys create --user <id> --expiration 1h
```

Put the key in `.env` as `TS_AUTHKEY`, start the node, then remove the key from `.env` (the
node keeps its login in `data/tailscale`):

```sh
docker compose up -d tailscale tailscale-nat
docker compose exec tailscale tailscale status            # this node, 100.64.0.1
```

On the home server: [`home/README.md`](../examples/home/README.md), part 2 (another
pre-auth key). Then put `service: home` back in `routes.yml`: the failover services prefer
Tailscale and fall back to WireGuard.

For Headplane, create an API key (`docker compose exec headscale headscale apikeys create`) and
sign in at `https://hs.example.com:8443/admin` over the VPN.

**Check:**

```sh
docker compose exec traefik wget -qO- -T 5 http://100.64.0.10:8123/manifest.json | head -c 80
docker compose exec tailscale iptables -t nat -S POSTROUTING | grep tailscale0   # the NAT rule
```

Then the failover test: stop Tailscale at home (`docker compose -f compose.tailscale.yml stop`).
Within about 15 s (the health-check interval), `https://home.example.com` still works over
WireGuard. Start it again and Traefik goes back to Tailscale.

## 6. CrowdSec

Everything is in [`compose.crowdsec.yml`](../examples/compose.crowdsec.yml); the background is in
authelia-webui's [hardening guide](https://github.com/shuricksumy/authelia-webui/blob/main/docs/hardening.md),
section 2.

```sh
KEY=$(openssl rand -hex 32); sed -i "s/^CROWDSEC_BOUNCER_KEY=.*/CROWDSEC_BOUNCER_KEY=$KEY/" .env
printf '%s' "$KEY" | sudo tee traefik/crowdsec-bouncer.key >/dev/null; unset KEY
sudo chown root:root traefik/crowdsec-bouncer.key && sudo chmod 600 traefik/crowdsec-bouncer.key
sudo mkdir -p data/crowdsec/config data/crowdsec/data
cp traefik/optional/security.yml traefik/dynamic/
sed -i 's/^COMPOSE_FILE=.*/&:compose.crowdsec.yml/' .env
```

In `traefik/traefik.yml`, uncomment the `http:` block under `websecure` (the two middlewares) and
the `experimental:` plugins block at the end. Then CrowdSec first, Traefik after it:

```sh
docker compose up -d crowdsec
docker compose exec crowdsec cscli lapi status                # "successfully interact"
docker compose up -d --force-recreate traefik                 # static config: read at start only
```

**Check:** `docker compose exec crowdsec cscli bouncers list` shows `traefik` with a recent
pull (open a site once). From a phone on **mobile data, VPN off**:

```sh
docker compose exec crowdsec cscli decisions add --ip <phone-ip> --duration 5m
# within ~60 s every public site answers 403 on the phone
docker compose exec crowdsec cscli decisions delete --ip <phone-ip>
```

More: a **firewall bouncer** (bans on every port, SSH and the VPN included), blocklists, and a
CDN in front: authelia-webui's [firewall-bouncer.md](https://github.com/shuricksumy/authelia-webui/blob/main/docs/firewall-bouncer.md)
and [blocking.md](https://github.com/shuricksumy/authelia-webui/blob/main/docs/blocking.md).

## 7. Traefik Manager

Follow the commands at the top of [`compose.traefik-manager.yml`](../examples/compose.traefik-manager.yml)
(they add it to `COMPOSE_FILE`, copy its route file, and prepare its folders). Its first-login
password is in its log. In its setup wizard, the Traefik API URL is `http://172.30.0.2:8081` (an
address, not a name), and **skip "Self route"**.

**Check:** over the VPN, `https://tm.example.com:8443` asks for the Authelia login, then Traefik
Manager's; its route map shows your routers. It edits `routes.yml` only: `core.yml`, `admin.yml`
and `security.yml` stay hand-edited, so it can never remove the protection of the admin pages.
When you add a route there, pick the `authelia` middleware unless the app cannot pass a login page.

Details and pitfalls: authelia-webui's [traefik-manager.md](https://github.com/shuricksumy/authelia-webui/blob/main/docs/traefik-manager.md).

## 8. The start page: foyer

```sh
git clone --depth 1 https://github.com/shuricksumy/foyer data/foyer
docker compose up -d foyer
```

Edit `foyer/config.yaml` (your services; it reloads on refresh). trapdoor and the configurator
already allow being shown inside `https://start.example.com` (`TD_FRAME_ANCESTORS`,
`FRAME_ANCESTORS`).

**Check:** over the VPN, `https://start.example.com:8443` shows the sidebar; *VPN (trapdoor)*
opens inside the page.

## 9. ZeroTier (optional)

```sh
sed -i 's/^COMPOSE_FILE=.*/&:compose.zerotier.yml/' .env
cp traefik/optional/zerotier.yml traefik/dynamic/
mkdir -p data/zerotier && docker compose up -d
```

Over the VPN, open `https://zt.example.com:8443`, create the first ZTNET user, then a network
with the range `10.147.17.0/24` (or change `ZT_SUBNET` to the range you choose). Make the
gateway a member of its own network and authorize it in ZTNET:

```sh
docker compose exec zerotier zerotier-cli join <network-id>
docker compose exec zerotier zerotier-cli listnetworks      # OK, with its 10.147.17.x address
```

Join the home server too (the ZeroTier client, authorized in ZTNET). Add its address as a third
server in a service's failover chain, or use it for a second site.

## Final checklist

```sh
docker compose ps                                          # everything up, health checks healthy
docker compose exec traefik ip route                       # one route per tunnel range
docker compose exec crowdsec cscli bouncers list           # recent pull
```

- Off the VPN: the admin names do not resolve, and `https://<server-ip>:8443` times out.
- On the VPN: every admin page asks for the Authelia login (2FA), then its own.
- Public services work from mobile data; stopping one tunnel at home changes nothing visible.

## Rolling back a phase

| Phase | Undo |
| :-- | :-- |
| 2–9 | remove its file from `COMPOSE_FILE`, then `docker compose up -d --remove-orphans`; delete the route file it copied into `traefik/dynamic/` |
| 4 | point the routers back at what worked; delete the client in trapdoor |
| 6 | also comment out the CrowdSec lines in `traefik/traefik.yml` again, then `docker compose up -d --force-recreate traefik` |

Keep a copy of `traefik/traefik.yml` before phase 6 (`cp traefik/traefik.yml ~/traefik.yml.bak`).
