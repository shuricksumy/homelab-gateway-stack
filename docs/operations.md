# Operations

Keeping it running: updates, backups, logs, and what to do when something breaks.

## Updates

All images are pinned in the compose files and `.env`. Update deliberately, one part at a time,
and re-run that part's check from [deploy.md](deploy.md).

```sh
cd /opt/gateway
# change the version in .env or the compose file, then:
docker compose pull && docker compose up -d
docker compose ps                       # everything up, healthy
docker compose exec traefik ip route    # the tunnel routes are back
```

| Part | Where the version is | Notes |
| :-- | :-- | :-- |
| Authelia + configurator | `AUTHELIA_VERSION` | one value for both; the configurator refuses to apply while they differ |
| trapdoor | `TD_IMAGE` | `:0.4` follows patch releases; release notes in trapdoor's `docs/releases/` |
| Headscale / Headplane / Tailscale | `HEADSCALE_VERSION`, ... | read Headscale's changelog: config keys move between releases |
| Traefik, CrowdSec, Traefik Manager, plugins | the compose files, `traefik.yml` | Traefik's static config changes need `--force-recreate traefik` |

**After any `docker compose up` that recreates a tunnel container, check its fixed address**
(the `docker inspect` line in [tunnels.md](tunnels.md#-routes)). A tunnel container on the wrong
address breaks every route through it, silently.

The host itself: unattended security upgrades (`sudo apt install unattended-upgrades`), and a
reboot when `/var/run/reboot-required` exists.

## Backups

| What | How |
| :-- | :-- |
| Authelia: rules, users, OIDC clients, keys, history | the configurator: History → Backup |
| trapdoor: VPN servers, keys, clients, traffic history | trapdoor: Account → Scheduled backups (encrypted with `TD_BACKUP_PASSPHRASE`), or *Download backup* per server |
| Everything else | `.env`, `authelia/configuration.yml`, `traefik/`, `headscale/`, `headplane/`, `foyer/`, `data/headscale`, `data/tailscale`, `data/adguard/conf` |

A simple host backup of the rest, kept off the server:

```sh
sudo tar -czf /root/gateway-$(date +%F).tgz -C /opt gateway \
  --exclude=gateway/data/logs --exclude=gateway/data/crowdsec/data
```

The archive holds secrets: encrypt it before it leaves the server.

## Disk and memory on a small server

- **Logs:** every service in the compose files rotates its Docker log (`max-size: 15m`,
  `max-file: 3`). Traefik's access log and Authelia's log are files: rotate them with logrotate
  (authelia-webui's `examples/production/logrotate/`).
- **Images:** old images pile up after updates. Prune on a schedule (weekly cron):
  ```sh
  docker image prune -a -f --filter "until=168h"
  docker builder prune -f
  ```
- **Memory:** `docker stats --no-stream` shows each container. The whole stack without ZeroTier
  stays under 1 GB; add a 1–2 GB swap file on a 1–2 GB server anyway.

## Logs

| Question | Command |
| :-- | :-- |
| Is a route or a certificate failing? | `docker compose logs traefik --since 10m` |
| Why was someone refused? | `tail -f data/logs/traefik/access.log \| jq -r '[.ClientHost, .RequestHost, .DownstreamStatus] \| @tsv'` |
| Who is banned, and why? | `docker compose exec crowdsec cscli decisions list`, `cscli alerts list` |
| Who signed in, from where? | the configurator's Logs page, or `authelia/logs/authelia.log` |
| Who is on the VPN, how much traffic? | trapdoor: Overview, Connected, Traffic |
| Is a tunnel up? | `docker compose exec trapdoor-vpn wg show`, `docker compose exec tailscale tailscale status` |

## When something breaks

| Symptom | Likely cause | Fix |
| :-- | :-- | :-- |
| A home service times out; the tunnel shows a recent handshake | no NAT into the tunnel, or the route points at a wrong address | [tunnels.md](tunnels.md): check ①, ② |
| **400 Bad Request** from Home Assistant or Nextcloud | the app does not trust the tunnel's address | add the tunnel ranges to its trusted proxies ([home/README.md](../examples/home/README.md)) |
| Every service fell back to WireGuard | the Tailscale node is down or logged out | `docker compose exec tailscale tailscale status`; re-login with a new pre-auth key |
| Admin pages: name does not resolve on the phone | the phone does not use AdGuard | the VPN client's DNS must be `172.30.0.53`; trapdoor's DNS redirect preset catches apps that ignore it |
| Admin pages: 403 although on the VPN | the source is not a VPN range (NAT between the VPN and the edge network) | `docker compose exec trapdoor-vpn iptables -t nat -S`: no MASQUERADE `-o` the edge interface; the default route on the default network (`gw_priority`) |
| Every site 403 for one visitor | banned by CrowdSec | `cscli decisions list -i <ip>`; `cscli decisions delete -i <ip>`; add trusted ranges to `clientTrustedIPs` |
| Certificates not renewing | the DNS token expired, or a rate limit | `docker compose logs traefik \| grep -i acme` |
| Everyone signed out after a rule change | expected without Redis; with Redis it should not happen | check that Redis runs and Authelia's `session.redis` is set |
| Traefik ignores a change in `traefik.yml` | static config is read at start only | `docker compose up -d --force-recreate traefik` |

## Day to day

- **A new device on the VPN:** trapdoor → Clients → Add; the QR code in the WireGuard app.
- **A new family member:** configurator → Users; put them in `family`.
- **A new home service:** the checklist in [tunnels.md](tunnels.md#adding-a-service-the-checklist).
- **Who got banned this week:** `docker compose exec crowdsec cscli alerts list --since 168h`.
