# A working example

The whole gateway as compose files you can deploy, with placeholders (`example.com`,
`203.0.113.10`, `172.30.0.0/24`) instead of real values. Start with the core and add one part
at a time, in the order of **[docs/deploy.md](../docs/deploy.md)**: each phase ends with a check.

```sh
cp .env.example .env                # your domain, addresses, secrets
# docs/deploy.md, phase 0 and 1, then:
docker compose config -q && docker compose up -d
```

Compose reads `COMPOSE_FILE` from `.env`, so the parts you added are all covered by plain
`docker compose ...`:

```sh
COMPOSE_FILE=compose.yml:compose.trapdoor.yml:compose.headscale.yml:compose.crowdsec.yml:compose.traefik-manager.yml
```

## What is in it

| File | Services | Role |
| :-- | :-- | :-- |
| `compose.yml` | `traefik`, `traefik-routes` | the front door; routes into the tunnels |
| | `authelia`, `redis`, `configurator`, `socket-proxy` | login with 2FA; its rules and users in a browser |
| | `adguard` | DNS for VPN clients: the admin names, ad blocking |
| | `foyer` | the start page |
| `compose.trapdoor.yml` | `trapdoor-vpn`, `trapdoor-agent`, `trapdoor-web` | WireGuard / OpenVPN for your devices and the home server |
| `compose.headscale.yml` | `headscale`, `headplane`, `tailscale`, `tailscale-nat` | the second tunnel, and its control server |
| `compose.crowdsec.yml` | `crowdsec` | automatic bans, enforced in Traefik |
| `compose.traefik-manager.yml` | `traefik-manager` | routes in a browser |
| `compose.zerotier.yml` | `zerotier`, `zerotier-nat`, `ztnet`, `ztnet-postgres` | optional third path |
| `home/` | | what runs on the home server |

Configuration next to them:

| Path | |
| :-- | :-- |
| `traefik/traefik.yml` | entry points (public `:443`, VPN-only `:8443`), certificates (DNS challenge), plugins |
| `traefik/dynamic/core.yml` | the login portal, the configurator, the `authelia` and `vpn-only` middlewares |
| `traefik/dynamic/admin.yml` | the admin pages, on the VPN-only entry point |
| `traefik/dynamic/routes.yml` | your home services, each with Tailscale → WireGuard failover |
| `traefik/optional/` | route files copied into `dynamic/` in their phase |
| `traefik/routes.sh` | the routes into the tunnels |
| `scripts/tunnel-nat.sh`, `trapdoor/wg0.*.sh` | NAT into the tunnels |
| `authelia/` | Authelia's configuration and starting rules (from authelia-webui) |
| `headscale/`, `headplane/` | their configuration |
| `crowdsec/acquis.yaml` | the logs CrowdSec reads |
| `foyer/config.yaml` | the start page's services |

Everything the stack writes goes to `data/` (gitignored): certificates, state, logs.

## Three things that will bite you

1. **The tunnel containers need their fixed addresses.** Traefik's routes point at them. Keep
   every address in `.env`, and look at them after each `docker compose up`
   ([tunnels.md](../docs/tunnels.md#-routes)).
2. **No NAT into a tunnel, no answers.** The home server must see the tunnel's address, not
   Traefik's Docker address. `wg0.up.sh` and the NAT helpers do it.
3. **Apps answer 400 until they trust the tunnel.** Home Assistant, Nextcloud and others check
   who sets `X-Forwarded-For` ([home/README.md](home/README.md)).

## Checked with

Docker Engine 28 / Compose 2.36, Traefik 3.7, Authelia 4.39, CrowdSec 1.8, trapdoor 0.4,
Headscale 0.26. The compose files pass `docker compose config` in every combination, and
Traefik loads the routes, the failover services and the tunnel routes as described. Your
cloud provider, DNS provider and home network are yours to check, phase by phase.
