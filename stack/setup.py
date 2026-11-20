#!/usr/bin/env python3
"""Homelab gateway: turns one profile (gateway.toml) into a ready-to-run deployment.

    ./setup.sh                    generate ../deploy from gateway.toml (safe to run again)
    ./setup.sh bootstrap          after `docker compose up -d`: first users, VPN server, tunnel keys
    ./setup.sh check              validate the generated deployment

Standard library only (Python 3.11+ for tomllib). Password hashes and keys are made with the
pinned Authelia / httpd images, so Docker must be available. setup.sh runs this file in a
container when the host's Python is older.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATES = HERE / "templates"

# Pinned versions. A profile may override any of them in [versions].
VERSIONS = {
    "traefik": "v3.7.15",
    "authelia": "4.39.28",
    "redis": "8-alpine",
    "socket_proxy": "1.13.1",
    "trapdoor": "0.4",
    "headscale": "0.26.1",
    "headplane": "0.6.1",
    "tailscale": "v1.84.0",
    "crowdsec": "v1.8.1",
    "traefik_manager": "1.15.2",
    "adguard": "v0.107.63",
    "nginx": "1.29-alpine",
    "zerotier": "1.14.2",
    "ztnet": "latest",
    "crowdsec_bouncer_plugin": "v1.8.1",
    "realip_plugin": "v1.0.4",
}
FOYER_REPO = "https://github.com/shuricksumy/foyer.git"
FOYER_TARBALL = "https://codeload.github.com/shuricksumy/foyer/tar.gz/refs/heads/main"

# Fixed addresses on the edge network: the n-th host of EDGE_SUBNET.
EDGE_HOSTS = {"traefik": 2, "traefik_manager": 20, "trapdoor": 30, "tailscale": 40, "zerotier": 41, "adguard": 53}

PARTS = ("trapdoor", "headscale", "crowdsec", "traefik_manager", "adguard", "foyer", "zerotier")


class ProfileError(Exception):
    pass


# ── output helpers ───────────────────────────────────────────────────────────────────────────

def say(msg: str) -> None:
    print(f"  {msg}")


def step(msg: str) -> None:
    print(f"\n▸ {msg}")


def write(path: Path, text: str, mode: int = 0o644, once: bool = False) -> bool:
    """Writes a generated file. once=True: only when missing (files the stack edits itself)."""
    if once and path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return True


def run(cmd: list[str], **kw) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])}...: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout


def yq(value: str) -> str:
    """A YAML double-quoted string."""
    return json.dumps(value)


# ── the profile ──────────────────────────────────────────────────────────────────────────────

def load_profile(path: Path) -> dict:
    if not path.exists():
        raise ProfileError(f"{path} not found: cp gateway.example.toml gateway.toml, then edit it")
    p = tomllib.loads(path.read_text())
    g = p.setdefault("gateway", {})
    for key in ("domain", "public_ip", "email"):
        if not g.get(key):
            raise ProfileError(f"[gateway] {key} is required")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", g["domain"]):
        raise ProfileError(f"[gateway] domain {g['domain']!r} does not look like a domain")
    ipaddress.ip_address(g["public_ip"])
    g.setdefault("timezone", "UTC")
    g.setdefault("http_port", 80)
    g.setdefault("https_port", 443)

    c = p.setdefault("certificates", {})
    c.setdefault("challenge", "dns")
    if c["challenge"] not in ("dns", "http", "none"):
        raise ProfileError("[certificates] challenge must be dns, http or none")
    c.setdefault("dns_provider", "cloudflare")
    c.setdefault("dns_env", {})
    c.setdefault("staging", False)

    h = p.setdefault("hosts", {})
    for key, default in {
        "login": "auth", "access": "access", "trapdoor": "trapdoor", "traefik": "traefik",
        "adguard": "adguard", "start": "start", "headscale": "headscale", "ztnet": "ztnet",
    }.items():
        h[key] = fqdn(h.get(key, default), g["domain"])

    parts = p.setdefault("parts", {})
    for part in PARTS:
        parts.setdefault(part, part != "zerotier")
    unknown = set(parts) - set(PARTS)
    if unknown:
        raise ProfileError(f"[parts] unknown: {', '.join(sorted(unknown))}")

    n = p.setdefault("network", {})
    n.setdefault("edge", "172.28.0.0/24")
    n.setdefault("wireguard", "10.66.0.0/24")
    n.setdefault("wireguard_port", 51820)
    n.setdefault("tailscale", "100.64.0.0/10")
    n.setdefault("zerotier", "10.147.17.0/24")
    n.setdefault("home_lan", "")
    for key in ("edge", "wireguard", "tailscale", "zerotier") + (("home_lan",) if n["home_lan"] else ()):
        ipaddress.ip_network(n[key])
    edge = ipaddress.ip_network(n["edge"])
    if edge.num_addresses < 64:
        raise ProfileError("[network] edge needs at least a /26")
    n["ip"] = {k: str(edge.network_address + v) for k, v in EDGE_HOSTS.items()}
    n["dynamic"] = str(list(edge.subnets())[1])  # upper half: addresses Docker hands out

    home = p.setdefault("home", {})
    home.setdefault("wireguard", str(ipaddress.ip_network(n["wireguard"]).network_address + 10))
    home.setdefault("tailscale", "100.64.0.10")

    v = dict(VERSIONS)
    v.update(p.get("versions", {}))
    p["versions"] = v

    users = p.setdefault("users", [])
    if not users:
        raise ProfileError("add at least one [[users]] entry (yourself, in group admins)")
    for u in users:
        if not re.fullmatch(r"[a-z0-9._-]{2,32}", u.get("name", "")):
            raise ProfileError(f"user name {u.get('name')!r}: 2-32 of a-z 0-9 . _ -")
        u.setdefault("display_name", u["name"])
        u.setdefault("email", f"{u['name']}@{g['domain']}")
        u.setdefault("groups", [])
        u.setdefault("password", "")
    if not any("admins" in u["groups"] for u in users):
        raise ProfileError("at least one user needs the group 'admins'")

    for s in p.setdefault("services", []):
        if not re.fullmatch(r"[a-z0-9-]{2,40}", s.get("name", "")):
            raise ProfileError(f"service name {s.get('name')!r}: 2-40 of a-z 0-9 -")
        if not s.get("port"):
            raise ProfileError(f"service {s['name']}: port is required")
        s["host"] = fqdn(s.get("host", s["name"]), g["domain"])
        s.setdefault("title", s["name"].replace("-", " ").title())
        s.setdefault("scheme", "http")
        s.setdefault("health", "/")
        s.setdefault("address", "")  # a fixed address instead of the tunnels (e.g. a LAN device)
        if s["address"]:  # a device on the home LAN: reached through the route to home_lan
            s.setdefault("tailscale", "")
            s.setdefault("wireguard", "")
            ip = ipaddress.ip_address(s["address"])
            if not n["home_lan"] or ip not in ipaddress.ip_network(n["home_lan"]):
                raise ProfileError(f"service {s['name']}: address {ip} is not in [network] home_lan")
        s.setdefault("tailscale", home["tailscale"] if parts["headscale"] else "")
        s.setdefault("wireguard", home["wireguard"] if parts["trapdoor"] else "")
        s.setdefault("prefer", "tailscale")
        s.setdefault("login", True)
        s.setdefault("public", True)
        s.setdefault("groups", ["admins"])
        s.setdefault("icon", "")
        if not (s["tailscale"] or s["wireguard"] or s["address"]):
            raise ProfileError(f"service {s['name']}: no tailscale, wireguard or address")
    return p


def fqdn(name: str, domain: str) -> str:
    return name if "." in name else f"{name}.{domain}"


# ── secrets ──────────────────────────────────────────────────────────────────────────────────

def load_env(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'")
    return env


def secrets_for(p: dict, out: Path) -> dict:
    """Generated once, kept in deploy/secrets.env; profile passwords win."""
    path = out / "secrets.env"
    s = load_env(path)
    gen = {
        "REDIS_PASSWORD": lambda: secrets.token_urlsafe(32),
        "CONFIGURATOR_SECRET_KEY": lambda: secrets.token_urlsafe(48),
        "AUTHELIA_JWT_SECRET": lambda: secrets.token_urlsafe(48),
        "AUTHELIA_SESSION_SECRET": lambda: secrets.token_urlsafe(48),
        "AUTHELIA_STORAGE_KEY": lambda: secrets.token_urlsafe(48),
        "AUTHELIA_OIDC_HMAC": lambda: secrets.token_urlsafe(48),
        "TD_AGENT_TOKEN": lambda: secrets.token_hex(32),
        "TD_WEB_API_TOKEN": lambda: secrets.token_hex(32),
        "TD_BACKUP_PASSPHRASE": lambda: secrets.token_urlsafe(24),
        "CROWDSEC_BOUNCER_KEY": lambda: secrets.token_hex(32),
        "HEADPLANE_COOKIE_SECRET": lambda: secrets.token_hex(16),  # exactly 32 characters
        "ZTNET_POSTGRES_PASSWORD": lambda: secrets.token_hex(24),
        "ZTNET_NEXTAUTH_SECRET": lambda: secrets.token_urlsafe(32),
    }
    for key, make in gen.items():
        s.setdefault(key, make())
    for u in p["users"]:
        key = f"PASSWORD_{u['name'].upper().replace('-', '_').replace('.', '_')}"
        if u["password"]:
            s[key] = u["password"]
        s.setdefault(key, secrets.token_urlsafe(12))
        u["password"] = s[key]
    s.setdefault("TS_AUTHKEY", "")
    body = "# Generated by setup.sh, kept between runs. Keep this file private (mode 600).\n"
    body += "".join(f"{k}={v}\n" for k, v in s.items())
    write(path, body, 0o600)
    return s


def argon2_hash(p: dict, out: Path, user: dict) -> str:
    """Authelia's own argon2id hash, cached (a hash costs a container start)."""
    cache = out / "secrets" / "hashes.json"
    data = json.loads(cache.read_text()) if cache.exists() else {}
    fp = hashlib.sha256(f"{user['name']}:{user['password']}".encode()).hexdigest()
    if data.get(f"authelia:{user['name']}", {}).get("fp") != fp:
        txt = run(["docker", "run", "--rm", f"authelia/authelia:{p['versions']['authelia']}",
                   "authelia", "crypto", "hash", "generate", "argon2", "--password", user["password"]])
        digest = re.search(r"Digest: (\S+)", txt).group(1)
        data[f"authelia:{user['name']}"] = {"fp": fp, "hash": digest}
        write(cache, json.dumps(data, indent=1), 0o600)
    return data[f"authelia:{user['name']}"]["hash"]


def bcrypt_hash(out: Path, user: dict) -> str:
    """bcrypt for AdGuard Home's login, cached."""
    cache = out / "secrets" / "hashes.json"
    data = json.loads(cache.read_text()) if cache.exists() else {}
    fp = hashlib.sha256(f"{user['name']}:{user['password']}".encode()).hexdigest()
    if data.get(f"bcrypt:{user['name']}", {}).get("fp") != fp:
        line = run(["docker", "run", "--rm", "httpd:2.4-alpine", "htpasswd", "-nbB", user["name"], user["password"]])
        data[f"bcrypt:{user['name']}"] = {"fp": fp, "hash": line.strip().split(":", 1)[1]}
        write(cache, json.dumps(data, indent=1), 0o600)
    return data[f"bcrypt:{user['name']}"]["hash"]


# ── generators ───────────────────────────────────────────────────────────────────────────────

def tls_of(p: dict) -> str:
    return "{certResolver: le}" if p["certificates"]["challenge"] != "none" else "{}"


def vpn_cidrs(p: dict) -> list[str]:
    n, parts = p["network"], p["parts"]
    out = []
    if parts["trapdoor"]:
        out.append(n["wireguard"])
    if parts["headscale"]:
        out.append(n["tailscale"])
    if parts["zerotier"]:
        out.append(n["zerotier"])
    return out


def admin_hosts(p: dict) -> list[tuple[str, str, str]]:
    """(key, host, title) of every VPN-only admin page that is enabled."""
    h, parts = p["hosts"], p["parts"]
    out = [("access", h["access"], "Access rules (Authelia)")]
    if parts["trapdoor"]:
        out.append(("trapdoor", h["trapdoor"], "VPN (trapdoor)"))
    if parts["traefik_manager"]:
        out.append(("traefik", h["traefik"], "Routes (Traefik Manager)"))
    if parts["adguard"]:
        out.append(("adguard", h["adguard"], "DNS (AdGuard Home)"))
    if parts["foyer"]:
        out.append(("start", h["start"], "Start page"))
    if parts["zerotier"]:
        out.append(("ztnet", h["ztnet"], "ZeroTier (ZTNET)"))
    return out


def gen_env(p: dict, s: dict, out: Path) -> None:
    g, n, h, v, parts = p["gateway"], p["network"], p["hosts"], p["versions"], p["parts"]
    files = ["compose.core.yml"] + [f"compose.{part.replace('_', '-')}.yml" for part in PARTS if parts[part]]
    routes = []
    if parts["trapdoor"]:
        routes.append(f"{n['wireguard']}={n['ip']['trapdoor']}")
        if n["home_lan"]:
            routes.append(f"{n['home_lan']}={n['ip']['trapdoor']}")
    if parts["headscale"]:
        routes.append(f"{n['tailscale']}={n['ip']['tailscale']}")
    if parts["zerotier"]:
        routes.append(f"{n['zerotier']}={n['ip']['zerotier']}")
    try:  # the group that may use the Docker socket (the socket proxy runs with it)
        docker_gid = 0 if sys.platform == "darwin" else os.stat("/var/run/docker.sock").st_gid
    except OSError:  # Docker Desktop (macOS): the socket in its VM belongs to root
        docker_gid = 0
    admin = next(u for u in p["users"] if "admins" in u["groups"])
    env = {
        "COMPOSE_FILE": ":".join(files),
        "DOMAIN": g["domain"], "TZ": g["timezone"],
        "HTTP_PORT": g["http_port"], "HTTPS_PORT": g["https_port"],
        "HOST_LOGIN": h["login"], "HOST_ACCESS": h["access"], "HOST_TRAPDOOR": h["trapdoor"],
        "HOST_ZTNET": h["ztnet"],
        "EDGE_SUBNET": n["edge"], "EDGE_DYNAMIC_RANGE": n["dynamic"], "WG_PORT": n["wireguard_port"],
        "IP_TRAEFIK": n["ip"]["traefik"], "IP_TRAEFIK_MANAGER": n["ip"]["traefik_manager"],
        "IP_TRAPDOOR": n["ip"]["trapdoor"], "IP_TAILSCALE": n["ip"]["tailscale"],
        "IP_ZEROTIER": n["ip"]["zerotier"], "IP_ADGUARD": n["ip"]["adguard"],
        "TUNNEL_ROUTES": " ".join(routes), "VPN_CIDRS": ",".join(vpn_cidrs(p)) or "127.0.0.1/32",
        "ADMIN_GROUP": "admins", "ADMIN_USER": admin["name"],
        "FRAME_ANCESTORS": f"https://{h['start']}:8443" if parts["foyer"] else "",
        "DOCKER_GID": docker_gid,
    }
    env.update({f"{k.upper()}_VERSION": val for k, val in v.items()})
    secret_keys = ["REDIS_PASSWORD", "CONFIGURATOR_SECRET_KEY", "TD_AGENT_TOKEN", "TD_WEB_API_TOKEN",
                   "TD_BACKUP_PASSPHRASE", "CROWDSEC_BOUNCER_KEY", "ZTNET_POSTGRES_PASSWORD",
                   "ZTNET_NEXTAUTH_SECRET", "TS_AUTHKEY"]
    env.update({k: s[k] for k in secret_keys})
    body = "# Generated by setup.sh from gateway.toml: change the profile and run setup.sh again.\n"
    body += "".join(f"{k}={val}\n" for k, val in env.items())
    write(out / ".env", body, 0o600)
    dns_env = "".join(f"{k}={val}\n" for k, val in p["certificates"]["dns_env"].items())
    write(out / "dns-provider.env", "# Token(s) of your DNS provider for certificates (from gateway.toml)\n" + dns_env, 0o600)
    for f in files:
        shutil.copy(TEMPLATES / "compose" / f.removeprefix("compose."), out / f)


def gen_traefik(p: dict, s: dict, out: Path) -> None:
    g, c, h, n, parts, v = p["gateway"], p["certificates"], p["hosts"], p["network"], p["parts"], p["versions"]
    t = out / "traefik"
    tls = tls_of(p)
    cs = parts["crowdsec"]
    static = [
        "# Traefik static configuration, generated by setup.sh. Read at start only:",
        "# after a change, docker compose up -d --force-recreate traefik",
        "entryPoints:",
        "  web:",
        '    address: ":80"',
        "    http:",
        "      redirections:",
        "        entryPoint: {to: websecure, scheme: https}",
        "  websecure: # public",
        '    address: ":443"',
    ]
    static += ["    http:", "      aliasHeadersStrategy: delete # X_Auth_User must not pass as X-Auth-User"]
    if cs:
        static.append("      middlewares: [realip@file, crowdsec@file] # before every router, so before Authelia")
    static += [
        "  internal: # VPN-only: never published; VPN clients reach Traefik's edge address directly",
        '    address: ":8443"',
        "    http:",
        "      aliasHeadersStrategy: delete",
    ]
    if parts["traefik_manager"]:
        static += ["  tmapi: # Traefik's API for Traefik Manager only; never published", '    address: ":8081"']
    static += ["", "api:", "  dashboard: false", ""]
    if c["challenge"] != "none":
        static += ["certificatesResolvers:", "  le:", "    acme:", f"      email: {g['email']}",
                   "      storage: /letsencrypt/acme.json"]
        if c["staging"]:
            static.append("      caServer: https://acme-staging-v02.api.letsencrypt.org/directory")
        if c["challenge"] == "dns":
            static += ["      dnsChallenge: # the VPN-only names are not reachable from the internet",
                       f"        provider: {c['dns_provider']}"]
        else:
            static += ["      httpChallenge: {entryPoint: web} # VPN-only names get no certificate"]
        static.append("")
    static += ["providers:", "  file:", "    directory: /etc/traefik/dynamic", "    watch: true", "",
               "log:", "  level: INFO", "", "accessLog:", "  filePath: /var/log/traefik/access.log",
               "  format: json"]
    if cs:
        static += ["", "experimental:", "  plugins:", "    bouncer:",
                   "      moduleName: github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin",
                   f"      version: {v['crowdsec_bouncer_plugin']}", "    realip:",
                   "      moduleName: github.com/xethlyx/traefik-real-ip",
                   f"      version: {v['realip_plugin']}"]
    write(t / "traefik.yml", "\n".join(static) + "\n")
    shutil.copy(TEMPLATES / "scripts" / "routes.sh", t / "routes.sh")

    d = t / "dynamic"
    for old in ("security.yml", "traefik-manager.yml", "headscale.yml", "admin.yml"):
        (d / old).unlink(missing_ok=True)  # parts that were switched off
    vpn = vpn_cidrs(p) or ["127.0.0.1/32"]
    core = f"""# Generated by setup.sh: the login portal, the configurator, the shared middlewares.
http:
  routers:
    authelia: # the login portal: public
      rule: Host(`{h['login']}`)
      entryPoints: [websecure]
      service: authelia
      tls: {tls}
    configurator: # VPN-only + allow-list + Authelia (admins, 2FA)
      rule: Host(`{h['access']}`)
      entryPoints: [internal]
      service: configurator
      middlewares: [vpn-only, authelia]
      tls: {tls}
  middlewares:
    authelia:
      forwardAuth:
        address: http://authelia:9091/api/authz/forward-auth
        trustForwardHeader: true
        authResponseHeaders: [Remote-User, Remote-Groups, Remote-Email, Remote-Name]
    vpn-only: # VPN clients reach Traefik without NAT, so this is their own address
      ipAllowList:
        sourceRange: {json.dumps(vpn)}
  services:
    authelia: {{loadBalancer: {{servers: [{{url: "http://authelia:9091"}}]}}}}
    configurator: {{loadBalancer: {{servers: [{{url: "http://configurator:8080"}}]}}}}
"""
    write(d / "core.yml", core)

    admin = {"trapdoor": "http://trapdoor-web:8080", "adguard": "http://adguard:3000",
             "start": "http://foyer:80", "ztnet": "http://ztnet:3000"}
    routers, services = [], []
    for key, host, _ in admin_hosts(p):
        if key not in admin:
            continue
        routers.append(f"""    {key}:
      rule: Host(`{host}`)
      entryPoints: [internal]
      service: {key}
      middlewares: [vpn-only, authelia]
      tls: {tls}""")
        services.append(f'    {key}: {{loadBalancer: {{servers: [{{url: "{admin[key]}"}}]}}}}')
    if routers:
        write(d / "admin.yml", "# Generated by setup.sh: the VPN-only admin pages.\nhttp:\n  routers:\n"
              + "\n".join(routers) + "\n  services:\n" + "\n".join(services) + "\n")

    if parts["headscale"]:
        write(d / "headscale.yml", f"""# Generated by setup.sh. Headscale must be public (every Tailscale client talks to it);
# Headplane, its admin UI, is VPN-only.
http:
  routers:
    headscale:
      rule: Host(`{h['headscale']}`)
      entryPoints: [websecure]
      service: headscale
      tls: {tls}
    headplane:
      rule: Host(`{h['headscale']}`) && PathPrefix(`/admin`)
      entryPoints: [internal]
      service: headplane
      middlewares: [vpn-only, authelia]
      tls: {tls}
  services:
    headscale: {{loadBalancer: {{servers: [{{url: "http://headscale:8080"}}]}}}}
    headplane: {{loadBalancer: {{servers: [{{url: "http://headplane:3000"}}]}}}}
""")

    if cs:
        trusted = ["127.0.0.1/32", n["edge"]] + vpn_cidrs(p) + ([n["home_lan"]] if n["home_lan"] else [])
        write(d / "security.yml", f"""# Generated by setup.sh: CrowdSec's bouncer and realip, first on the public entry point.
# Background: authelia-webui's docs/hardening.md.
http:
  middlewares:
    realip:
      plugin:
        realip:
          trustedIPs: &cdn
            - 192.0.2.0/24 # your CDN's ranges if one is in front; this placeholder trusts nothing
    crowdsec:
      plugin:
        bouncer:
          enabled: true
          logLevel: INFO
          crowdsecMode: stream
          crowdsecLapiScheme: http
          crowdsecLapiHost: crowdsec:8080
          crowdsecLapiKeyFile: /etc/traefik/crowdsec-bouncer.key
          updateMaxFailure: -1 # CrowdSec down: let traffic through
          forwardedHeadersTrustedIPs: *cdn
          clientTrustedIPs: {json.dumps(trusted)} # never banned
""")
        write(t / "crowdsec-bouncer.key", s["CROWDSEC_BOUNCER_KEY"], 0o600)
        write(out / "crowdsec" / "acquis.yaml", "filenames: [/var/log/traefik/access.log]\nlabels: {type: traefik}\n"
              "---\nfilenames: [/var/log/authelia/authelia.log]\nlabels: {type: authelia}\n")

    if parts["traefik_manager"]:
        write(d / "traefik-manager.yml", f"""# Generated by setup.sh. Kept out of services.yml on purpose: Traefik Manager edits only
# that file, so it cannot remove its own protection or widen its API access.
http:
  routers:
    traefik-manager:
      rule: Host(`{h['traefik']}`)
      entryPoints: [internal]
      service: traefik-manager
      middlewares: [vpn-only, authelia]
      tls: {tls}
    traefik-manager-api:
      rule: PathPrefix(`/api`)
      entryPoints: [tmapi]
      service: api@internal
      middlewares: [traefik-manager-only]
  middlewares:
    traefik-manager-only:
      ipAllowList:
        sourceRange: ["{n['ip']['traefik_manager']}/32"]
  services:
    traefik-manager: {{loadBalancer: {{servers: [{{url: "http://traefik-manager:5000"}}]}}}}
""")
        # Comments only: an empty `http:` map is an error that stops Traefik's whole file provider.
        write(d / "services.yml", "# Your own routes, edited in Traefik Manager (or by hand). setup.sh never overwrites\n"
              "# this file; the services of gateway.toml are in profile-services.yml.\n", once=True)

    gen_profile_services(p, d)


def gen_profile_services(p: dict, d: Path) -> None:
    tls = tls_of(p)
    routers, services = [], []
    for sv in p["services"]:
        name = sv["name"]
        mw = (["vpn-only"] if not sv["public"] else []) + (["authelia"] if sv["login"] else [])
        r = [f"    {name}:", f"      rule: Host(`{sv['host']}`)",
             f"      entryPoints: [{'websecure' if sv['public'] else 'internal'}]",
             f"      service: {name}", f"      tls: {tls}"]
        if mw:
            r.insert(4, f"      middlewares: {json.dumps(mw)}")
        routers.append("\n".join(r))
        hc = f"healthCheck: {{path: {yq(sv['health'])}, interval: 15s, timeout: 3s}}"
        paths = {}
        if sv["address"]:
            paths["lan"] = sv["address"]
        if sv["tailscale"]:
            paths["ts"] = sv["tailscale"]
        if sv["wireguard"]:
            paths["wg"] = sv["wireguard"]
        if len(paths) == 1:
            (addr,) = paths.values()
            services.append(f"    {name}:\n      loadBalancer:\n        servers: [{{url: \"{sv['scheme']}://{addr}:{sv['port']}\"}}]\n"
                            f"        passHostHeader: true\n        {hc}")
            continue
        first, second = ("ts", "wg") if sv["prefer"] == "tailscale" else ("wg", "ts")
        if "lan" in paths:
            first, second = "lan", ("ts" if "ts" in paths else "wg")
        services.append(f"    {name}: # {first} first, {second} while {first} is down\n"
                        f"      failover: {{service: {name}-{first}, fallback: {name}-{second}}}")
        for key in (first, second):
            services.append(f"    {name}-{key}:\n      loadBalancer:\n        servers: [{{url: \"{sv['scheme']}://{paths[key]}:{sv['port']}\"}}]\n"
                            f"        passHostHeader: true\n        {hc}")
    body = ("# Generated by setup.sh from [[services]] in gateway.toml: change the profile, not this file.\n"
            "# Each service with two tunnels: the preferred one while its health check passes, the\n"
            "# other while it fails (docs/guide.md, Tunnels home).\n")
    if routers:
        body += "http:\n  routers:\n" + "\n".join(routers) + "\n  services:\n" + "\n".join(services) + "\n"
    write(d / "profile-services.yml", body)  # comments only without services: `http: {}` is an error


def gen_authelia(p: dict, s: dict, out: Path, reset: bool) -> None:
    g, h = p["gateway"], p["hosts"]
    a = out / "authelia"
    m = a / "managed"
    (a / "logs").mkdir(parents=True, exist_ok=True)
    smtp = p.get("smtp", {})
    if smtp.get("address"):
        notifier = f"""notifier:
  smtp:
    address: {yq(smtp['address'])}
    username: {yq(smtp.get('username', ''))}
    password: {yq(smtp.get('password', ''))}
    sender: {yq(smtp.get('sender', 'Authelia <' + g['email'] + '>'))}"""
    else:
        notifier = """notifier: # no [smtp] in the profile: messages (2FA registration links) go to this file
  filesystem:
    filename: /config/notification.txt"""
    start = f"https://{h['start']}:8443" if p["parts"]["foyer"] else f"https://{h['login']}"
    write(a / "configuration.yml", f"""# Generated by setup.sh from gateway.toml (secrets: secrets.env). Rules, users and OIDC clients
# are in managed/, edited in the configurator. Authelia reads every file through its template
# filter: never write two opening curly braces in a row here.
theme: auto
server:
  address: 'tcp://0.0.0.0:9091/'
log:
  level: info
  format: json
  file_path: /config/logs/authelia.log
  keep_stdout: true
totp:
  issuer: {g['domain']}
identity_validation:
  reset_password:
    jwt_secret: {yq(s['AUTHELIA_JWT_SECRET'])}
authentication_backend:
  file:
    path: /config/managed/users.yml
    watch: true
    search:
      email: true
    password:
      algorithm: argon2
password_policy:
  standard:
    enabled: true
    min_length: 12
session:
  secret: {yq(s['AUTHELIA_SESSION_SECRET'])}
  cookies:
    - domain: {yq(g['domain'])}
      authelia_url: 'https://{h['login']}'
      default_redirection_url: '{start}'
  redis:
    host: redis
    port: 6379
regulation:
  max_retries: 3
  find_time: 2m
  ban_time: 10m
storage:
  encryption_key: {yq(s['AUTHELIA_STORAGE_KEY'])}
  local:
    path: /config/db.sqlite3
{notifier}
identity_providers:
  oidc:
    hmac_secret: {yq(s['AUTHELIA_OIDC_HMAC'])}
""", 0o600)

    # managed/: the configurator owns these after the first run.
    vpn = vpn_cidrs(p) or ["127.0.0.1/32"]
    admin_list = [host for _, host, _ in admin_hosts(p) if host != h["access"]]
    rules = [f"""    - domain: {h['login']} # OIDC and discovery must stay public
      resources: ['^/\\.well-known/openid-configuration$', '^/jwks\\.json$', '^/api/oidc/.*']
      policy: bypass
    - domain: {h['login']}
      policy: one_factor
    - domain: {h['access']} # the configurator: admins, on the VPN, with 2FA
      networks: [vpn]
      subject: 'group:admins'
      policy: two_factor
    - domain: {h['access']}
      policy: deny"""]
    if admin_list:
        rules.append(f"""    - domain: {json.dumps(admin_list)} # the other admin pages: the same
      networks: [vpn]
      subject: 'group:admins'
      policy: two_factor""")
    if p["parts"]["headscale"]:
        rules.append(f"""    - domain: {h['headscale']} # Headplane
      resources: ['^/admin([/?].*)?$']
      networks: [vpn]
      subject: 'group:admins'
      policy: two_factor""")
    for sv in p["services"]:
        if sv["login"]:
            subj = json.dumps([f"group:{grp}" for grp in sv["groups"]])
            net = "\n      networks: [vpn]" if not sv["public"] else ""
            rules.append(f"""    - domain: {sv['host']} # {sv['title']}{net}
      subject: {subj}
      policy: two_factor""")
    write(m / "access_control.yml", f"""# Starting rules, generated once by setup.sh; edit them in the configurator.
access_control:
  default_policy: deny
  networks:
    - name: vpn
      networks: {json.dumps(vpn)}
  rules:
""" + "\n".join(rules) + "\n", once=not reset)

    users = ["users:"]
    for u in p["users"]:
        users += [f"  {u['name']}:", f"    displayname: {yq(u['display_name'])}", f"    email: {yq(u['email'])}",
                  f"    groups: {json.dumps(u['groups'])}", "    disabled: false",
                  f"    password: {yq(argon2_hash(p, out, u))}"]
    write(m / "users.yml", "# Generated once by setup.sh; manage users in the configurator.\n" + "\n".join(users) + "\n",
          0o600, once=not reset)
    write(m / "oidc_clients.yml", f"""# Authelia needs one client while OIDC is on; add real ones in the configurator (presets for
# common apps), then delete this placeholder there.
identity_providers:
  oidc:
    clients:
      - client_id: 'placeholder'
        client_name: 'Placeholder (delete me)'
        public: true
        authorization_policy: 'two_factor'
        require_pkce: true
        pkce_challenge_method: 'S256'
        redirect_uris: ['https://{h['login']}/placeholder']
        scopes: ['openid', 'profile']
        token_endpoint_auth_method: 'none'
""", once=not reset)
    write(m / "oidc_jwks.yml", """# The OIDC signing key; the private key is a file in secrets/jwks/ (rotate on the Keys page).
identity_providers:
  oidc:
    jwks:
      - key_id: 'main'
        algorithm: 'RS256'
        use: 'sig'
        key: {{ secret "/config/managed/secrets/jwks/main.pem" | mindent 10 "|" | msquote }}
""", once=not reset)
    key = m / "secrets" / "jwks" / "main.pem"
    if not key.exists():
        tmp = out / "secrets" / "jwks-tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        uid = f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0"
        run(["docker", "run", "--rm", "-u", uid, "-v", f"{tmp}:/out", f"authelia/authelia:{p['versions']['authelia']}",
             "authelia", "crypto", "pair", "rsa", "generate", "--bits", "2048", "--directory", "/out"])
        key.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(tmp / "private.pem", key)
        key.chmod(0o600)
        shutil.rmtree(tmp)


def gen_headscale(p: dict, s: dict, out: Path) -> None:
    h, n = p["hosts"], p["network"]
    write(out / "headscale" / "config.yaml", f"""# Generated by setup.sh. Compare with config-example.yaml of Headscale {p['versions']['headscale']}.
server_url: https://{h['headscale']}
listen_addr: 0.0.0.0:8080
metrics_listen_addr: 127.0.0.1:9090
grpc_listen_addr: 127.0.0.1:50443
noise:
  private_key_path: /var/lib/headscale/noise_private.key
prefixes:
  v4: {n['tailscale']}
  v6: fd7a:115c:a1e0::/48
  allocation: sequential
derp:
  server:
    enabled: false
  urls: [https://controlplane.tailscale.com/derpmap/default]
  auto_update_enabled: true
  update_frequency: 24h
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite
    write_ahead_log: true
dns:
  magic_dns: true
  base_domain: tailnet.internal
  override_local_dns: false
log:
  level: info
""")
    write(out / "headplane" / "config.yaml", f"""# Generated by setup.sh. Compare with config.example.yaml of Headplane {p['versions']['headplane']}.
server:
  host: 0.0.0.0
  port: 3000
  cookie_secret: {yq(s['HEADPLANE_COOKIE_SECRET'])}
  cookie_secure: true
headscale:
  url: http://headscale:8080
  public_url: https://{h['headscale']}
  config_path: /etc/headscale/config.yaml
  config_strict: true
integration:
  docker:
    enabled: false
""", 0o600)


def gen_adguard(p: dict, out: Path) -> None:
    """AdGuard Home's config, once: afterwards AdGuard owns it (its web UI edits it)."""
    path = out / "data" / "adguard" / "conf" / "AdGuardHome.yaml"
    if path.exists():
        return
    admin = next(u for u in p["users"] if "admins" in u["groups"])
    hosts = [host for _, host, _ in admin_hosts(p)] + [p["hosts"]["login"]]
    hosts += [sv["host"] for sv in p["services"]]
    if p["parts"]["headscale"]:
        hosts.append(p["hosts"]["headscale"])
    rewrites = "".join(f"    - domain: {host}\n      answer: {p['network']['ip']['traefik']}\n      enabled: true\n"
                       for host in sorted(set(hosts)))
    write(path, f"""# Generated once by setup.sh; AdGuard Home rewrites it with its own defaults on start.
http:
  address: 0.0.0.0:3000
users:
  - name: {admin['name']}
    password: {bcrypt_hash(out, admin)}
dns:
  bind_hosts: [0.0.0.0]
  port: 53
  upstream_dns: [https://dns.quad9.net/dns-query, https://cloudflare-dns.com/dns-query]
  bootstrap_dns: [9.9.9.9, 1.1.1.1]
filtering:
  rewrites: # every gateway name points at Traefik's internal address for VPN clients
{rewrites}schema_version: 29
""", 0o600)


def gen_foyer(p: dict, out: Path) -> None:
    app = out / "data" / "foyer"
    if not (app / "index.html").exists():
        say("downloading foyer (the start page)")
        tmp = out / "data" / "foyer-src"
        shutil.rmtree(tmp, ignore_errors=True)
        if shutil.which("git"):
            run(["git", "clone", "-q", "--depth", "1", FOYER_REPO, str(tmp)])
        else:  # curl is on nearly every server; Python's own HTTPS may lack CA certificates
            tmp.mkdir(parents=True)
            data = subprocess.run(["curl", "-fsSL", FOYER_TARBALL], capture_output=True, check=True).stdout
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(tmp, filter="data")
            (inner,) = list(tmp.iterdir())
            tmp = inner
        app.mkdir(parents=True, exist_ok=True)
        for name in ("index.html", "css", "js", "icons"):  # only what the page needs, never .git
            src = tmp / name
            (shutil.copytree if src.is_dir() else shutil.copy)(src, app / name)
        shutil.rmtree(out / "data" / "foyer-src", ignore_errors=True)

    have = {f.stem for f in (app / "icons").glob("*.svg")}

    def icon(name: str) -> str:  # an icon foyer does not have shows as a broken image
        return yq("local:" + (name if name in have else "server"))

    items = []
    for sv in p["services"]:
        port = "" if sv["public"] else ":8443"
        items.append(f"""      - name: {yq(sv['title'])}
        icon: {icon(sv['icon'])}
        embed: false
        link: {{ type: domain, value: "https://{sv['host']}{port}" }}""")
    icons = {"access": "authelia", "trapdoor": "wireguard", "traefik": "traefik-manager", "adguard": "adguard",
             "start": "home", "ztnet": "zerotier"}
    admin = []
    for key, host, title in admin_hosts(p):
        if key == "start":
            continue
        admin.append(f"""      - name: {yq(title)}
        icon: {icon(icons[key])}
        embed: {'true' if key in ('trapdoor', 'access') else 'false'}
        link: {{ type: domain, value: "https://{host}:8443" }}""")
    if p["parts"]["headscale"]:
        admin.append(f"""      - name: "Tailnet (Headplane)"
        icon: {icon("headscale")}
        embed: false
        link: {{ type: domain, value: "https://{p['hosts']['headscale']}:8443/admin" }}""")
    groups = []
    if items:
        groups.append("  - name: Services\n    icon: local:home\n    items:\n" + "\n".join(items))
    groups.append("  - name: Gateway\n    icon: local:server\n    items:\n" + "\n".join(admin))
    write(out / "foyer" / "config.yaml", "# Generated by setup.sh from gateway.toml. Options: foyer's own config.yaml.\n"
          f"title: {yq(p['gateway']['domain'])}\ntheme: dark\ngroups:\n" + "\n".join(groups) + "\n")


def gen_trapdoor(p: dict, out: Path) -> None:
    w = out / "data" / "trapdoor" / "wireguard"
    for d in ("openvpn", "wireguard", "amneziawg", "web"):
        (out / "data" / "trapdoor" / d).mkdir(parents=True, exist_ok=True)
    for f in ("wg0.up.sh", "wg0.down.sh"):
        shutil.copy(TEMPLATES / "scripts" / f, w / f)
        (w / f).chmod(0o755)


def gen_traefik_manager(p: dict, out: Path) -> None:
    vendor = out / "data" / "traefik-manager" / "vendor"
    for d in ("config", "backups", "vendor"):
        (out / "data" / "traefik-manager" / d).mkdir(parents=True, exist_ok=True)
    image = f"ghcr.io/chr0nzz/traefik-manager:{p['versions']['traefik_manager']}"
    for f in ("dagre.min.js", "qrcode.min.js"):
        if not (vendor / f).exists():  # the image ships them root-only: readable copies
            (vendor / f).write_text(run(["docker", "run", "--rm", "--entrypoint", "cat", image, f"/app/static/vendor/{f}"]))
            (vendor / f).chmod(0o644)


def gen_home(p: dict, out: Path) -> None:
    n, parts = p["network"], p["parts"]
    trust = [n["wireguard"]] if parts["trapdoor"] else []
    trust += [n["tailscale"]] if parts["headscale"] else []
    hm = out / "home"
    write(hm / "README.md", f"""# The home side

Copy this folder to your home server. It only dials out; nothing opens on the home router.

1. **WireGuard** (trapdoor): `wg-gateway.conf` appears here after `setup.sh bootstrap`.
   ```sh
   sudo apt install wireguard && sudo cp wg-gateway.conf /etc/wireguard/
   sudo systemctl enable --now wg-quick@wg-gateway
   ```
2. **Tailscale** (Headscale): after `setup.sh bootstrap`, `.env` here holds a one-time key.
   ```sh
   docker compose -f compose.tailscale.yml up -d
   ```
3. **Trust the gateway** in your apps, or they answer 400. Home Assistant, `configuration.yaml`:
   ```yaml
   http:
     use_x_forwarded_for: true
     trusted_proxies: {json.dumps(trust)}
   ```
""")
    if parts["headscale"]:
        write(hm / "compose.tailscale.yml", f"""# The home server's Tailscale node, joined to the gateway's Headscale.
services:
  tailscale:
    image: tailscale/tailscale:{p['versions']['tailscale']}
    container_name: tailscale-home
    hostname: home
    restart: unless-stopped
    network_mode: host
    cap_add: [NET_ADMIN, NET_RAW]
    devices: [/dev/net/tun]
    environment:
      TS_AUTHKEY: ${{TS_AUTHKEY}}
      TS_STATE_DIR: /var/lib/tailscale
      TS_USERSPACE: "false"
      TS_ACCEPT_DNS: "false"
      TS_EXTRA_ARGS: --login-server=https://{p['hosts']['headscale']}
    volumes:
      - ./tailscale-state:/var/lib/tailscale
""")


def fix_owners(p: dict, out: Path) -> None:
    """Containers run as fixed users; set owners when we can (as root on the server)."""
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        say("not root: skipping ownership (on the server, run setup.sh with sudo)")
        return
    own = {"authelia": 1000, "data/redis": 1000, "data/trapdoor/web": 10001}
    if p["parts"]["traefik_manager"]:
        own.update({"data/traefik-manager": 1000, "traefik/dynamic": 1000})
    for rel, uid in own.items():
        path = out / rel
        path.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(path):
            for x in [root] + [os.path.join(root, f) for f in dirs + files]:
                os.chown(x, uid, uid)
    key = out / "traefik" / "crowdsec-bouncer.key"
    if key.exists():
        os.chown(key, 0, 0)


# ── commands ─────────────────────────────────────────────────────────────────────────────────

def generate(args) -> None:
    p = load_profile(args.profile)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    print(f"Generating {out} from {args.profile.name}")
    step("secrets")
    s = secrets_for(p, out)
    say("secrets.env (kept between runs)")
    step("compose and .env")
    gen_env(p, s, out)
    (out / "scripts").mkdir(exist_ok=True)
    shutil.copy(TEMPLATES / "scripts" / "tunnel-nat.sh", out / "scripts" / "tunnel-nat.sh")
    for d in ("data/letsencrypt", "data/logs/traefik", "data/redis"):
        (out / d).mkdir(parents=True, exist_ok=True)
    say(".env: " + load_env(out / ".env")["COMPOSE_FILE"])
    step("Traefik")
    gen_traefik(p, s, out)
    say(f"{len(p['services'])} service(s) from the profile, routes: {load_env(out / '.env')['TUNNEL_ROUTES'] or 'none'}")
    step("Authelia (password hashes and the signing key use the Authelia image)")
    gen_authelia(p, s, out, args.reset_managed)
    if p["parts"]["trapdoor"]:
        step("trapdoor")
        gen_trapdoor(p, out)
    if p["parts"]["headscale"]:
        step("Headscale and Headplane")
        gen_headscale(p, s, out)
    if p["parts"]["adguard"]:
        step("AdGuard Home")
        gen_adguard(p, out)
    if p["parts"]["foyer"]:
        step("foyer")
        gen_foyer(p, out)
    if p["parts"]["traefik_manager"]:
        step("Traefik Manager")
        gen_traefik_manager(p, out)
    step("home side")
    gen_home(p, out)
    fix_owners(p, out)
    print(f"""
Done. Next:
  cd {out}
  docker compose up -d
  {HERE / 'setup.sh'} bootstrap      # first users, the VPN server, the tunnel keys""")


def compose(out: Path, *args: str, check: bool = True) -> str:
    res = subprocess.run(["docker", "compose", *args], cwd=out, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)}: {res.stderr.strip()}")
    return res.stdout


def edge_curl(out: Path, *args: str) -> str:
    """curl from a throwaway container on the edge network (works wherever setup.sh runs)."""
    return run(["docker", "run", "--rm", "--network", "edge", "curlimages/curl:8.11.1", "-fsS", "--max-time", "20", *args])


def wait_healthy(out: Path, service: str, timeout: int = 120) -> None:
    end = time.time() + timeout
    while time.time() < end:
        cid = compose(out, "ps", "-q", service, check=False).strip()
        if cid:
            st = run(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", cid]).strip()
            if st in ("healthy", "running"):
                return
        time.sleep(3)
    raise RuntimeError(f"{service} is not healthy after {timeout}s: docker compose logs {service}")


def bootstrap(args) -> None:
    p = load_profile(args.profile)
    out = args.out.resolve()
    s = secrets_for(p, out)
    n, parts, h = p["network"], p["parts"], p["hosts"]
    admin = next(u for u in p["users"] if "admins" in u["groups"])
    done = []

    if parts["trapdoor"]:
        step("trapdoor")
        wait_healthy(out, "trapdoor-web")
        users = compose(out, "exec", "-T", "trapdoor-web", "trapdoor", "user", "list")
        if not re.search(rf"^{re.escape(admin['name'])}\s", users, re.M):
            subprocess.run(["docker", "compose", "exec", "-T", "trapdoor-web", "trapdoor", "user", "add", admin["name"]],
                           cwd=out, input=admin["password"] + "\n", text=True, check=True, capture_output=True)
            say(f"admin {admin['name']} created (same password as Authelia)")
        api = ["-H", f"Authorization: Bearer {s['TD_WEB_API_TOKEN']}", "-H", "Content-Type: application/json"]
        servers = json.loads(edge_curl(out, *api, "http://trapdoor-web:8080/api/v1/servers"))
        if not any(x.get("id") == "wg0" for x in servers):
            body = {"type": "wireguard", "id": "wg0", "name": "Gateway", "port": n["wireguard_port"],
                    "public_host": p["gateway"]["public_ip"], "public_port": n["wireguard_port"], "subnet": n["wireguard"],
                    "dns": [n["ip"]["adguard"]] if parts["adguard"] else [],
                    "allowed_ips": [n["wireguard"], n["edge"]] + ([n["home_lan"]] if n["home_lan"] else []),
                    "keepalive": 25}
            edge_curl(out, *api, "-d", json.dumps(body), "http://trapdoor-web:8080/api/v1/servers")
            say(f"WireGuard server wg0: {n['wireguard']}, UDP {n['wireguard_port']}")
        # Saving the scripts settings once makes trapdoor's vpn container run them for wg0: the
        # presets (clients' DNS to AdGuard) and the own wg0.up.sh (NAT into the tunnel).
        sc = json.loads(edge_curl(out, *api, "http://trapdoor-web:8080/api/v1/servers/wg0/scripts"))
        want = {"dns_redirect": n["ip"]["adguard"] if parts["adguard"] else "", "block_dot": parts["adguard"],
                "isolate": sc.get("isolate", False), "up": sc.get("up", ""), "down": sc.get("down", "")}
        if sc.get("state") in (None, "", "pending") or sc.get("dns_redirect", "") != want["dns_redirect"]:
            edge_curl(out, *api, "-X", "PUT", "-d", json.dumps(want), "http://trapdoor-web:8080/api/v1/servers/wg0/scripts")
            say("scripts on: NAT into the tunnel" + (", clients' DNS to AdGuard" if parts["adguard"] else ""))
        peers = json.loads(edge_curl(out, *api, "http://trapdoor-web:8080/api/v1/servers/wg0/peers"))
        if not any(x.get("name") == "home" for x in peers):
            body = {"name": "home", "address": p["home"]["wireguard"], "allowed_ips": [n["wireguard"]], "keepalive": 25,
                    "notes": "the home server: the tunnel Traefik reaches home through"}
            if n["home_lan"]:
                body["lan"] = [n["home_lan"]]
            edge_curl(out, *api, "-d", json.dumps(body), "http://trapdoor-web:8080/api/v1/servers/wg0/peers")
            say(f"client 'home' at {p['home']['wireguard']}")
        conf = edge_curl(out, *api, "http://trapdoor-web:8080/api/v1/servers/wg0/peers/home/conf")
        write(out / "home" / "wg-gateway.conf", conf, 0o600)
        done.append("home/wg-gateway.conf: the home server's WireGuard config")
        # Your first device: the admin pages (trapdoor's own too) need the VPN.
        first = f"{admin['name']}-device"
        if not any(x.get("name") == first for x in peers):
            edge_curl(out, *api, "-d", json.dumps({"name": first, "notes": "first device, made by setup.sh bootstrap"}),
                      "http://trapdoor-web:8080/api/v1/servers/wg0/peers")
            say(f"client '{first}' for your first device")
        conf = edge_curl(out, *api, f"http://trapdoor-web:8080/api/v1/servers/wg0/peers/{first}/conf")
        write(out / "clients" / f"{first}.conf", conf, 0o600)
        done.append(f"clients/{first}.conf: import it in the WireGuard app (your first device)")

    if parts["headscale"]:
        step("Headscale")
        wait_healthy(out, "headscale")
        def hs(*a: str) -> str:
            return compose(out, "exec", "-T", "headscale", "headscale", *a, "-o", "json")
        users = json.loads(hs("users", "list") or "[]") or []
        uid = next((u["id"] for u in users if u.get("name") == "gateway"), None)
        if uid is None:
            uid = json.loads(hs("users", "create", "gateway"))["id"]
            say("user 'gateway' created")
        nodes = json.loads(hs("nodes", "list") or "[]") or []
        if not any(x.get("given_name") == "gateway" or x.get("name") == "gateway" for x in nodes):
            key = json.loads(hs("preauthkeys", "create", "--user", str(uid), "--expiration", "1h"))["key"]
            env = (out / ".env").read_text()
            (out / ".env").write_text(re.sub(r"^TS_AUTHKEY=.*$", f"TS_AUTHKEY={key}", env, flags=re.M))
            compose(out, "up", "-d", "tailscale")
            say("the gateway's Tailscale node is logging in")
        home_key = json.loads(hs("preauthkeys", "create", "--user", str(uid), "--expiration", "24h"))["key"]
        write(out / "home" / ".env", f"TS_AUTHKEY={home_key}\n", 0o600)
        apikey = compose(out, "exec", "-T", "headscale", "headscale", "apikeys", "create", "--expiration", "90d").strip()
        write(out / "secrets" / "headplane-api-key.txt", apikey + "\n", 0o600)
        done.append("home/.env: a Tailscale key for the home server (valid 24 h)")
        done.append("secrets/headplane-api-key.txt: Headplane's login (90 days)")

    step("summary")
    print(f"""
  Public:     https://{h['login']}            (sign in: {admin['name']} / password below)""")
    for sv in p["services"]:
        if sv["public"]:
            print(f"              https://{sv['host']}   {sv['title']}")
    print("  VPN-only (import clients/<you>-device.conf in the WireGuard app first):")
    for _, host, title in admin_hosts(p):
        print(f"              https://{host}:8443   {title}")
    for sv in p["services"]:
        if not sv["public"]:
            print(f"              https://{sv['host']}:8443   {sv['title']}")
    print("\n  Users and passwords (also in secrets.env):")
    for u in p["users"]:
        print(f"              {u['name']:<12} {u['password']}")
    print("\n  Files for you:")
    for d in done:
        print(f"              {d}")
    print(f"\n  First 2FA registration: the link is in {out}/authelia/notification.txt until [smtp] is set.")


def check(args) -> None:
    p = load_profile(args.profile)
    out = args.out.resolve()
    step("docker compose config")
    compose(out, "config", "-q")
    say("ok")
    step("authelia validate-config")
    a = out / "authelia"
    res = subprocess.run(["docker", "run", "--rm", "-v", f"{a}:/config",
                          "-e", "X_AUTHELIA_CONFIG_FILTERS=template",
                          f"authelia/authelia:{p['versions']['authelia']}", "authelia", "validate-config",
                          "--config", "/config/configuration.yml,/config/managed/access_control.yml,"
                          "/config/managed/oidc_clients.yml,/config/managed/oidc_jwks.yml"],
                         capture_output=True, text=True)
    print(textwrap_indent(res.stdout + res.stderr))
    if res.returncode != 0:
        sys.exit(1)


def textwrap_indent(t: str) -> str:
    return "\n".join("  " + line for line in t.strip().splitlines())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="generate", choices=["generate", "bootstrap", "check"])
    ap.add_argument("--profile", type=Path, default=HERE / "gateway.toml")
    ap.add_argument("--out", type=Path, default=HERE.parent / "deploy")
    ap.add_argument("--reset-managed", action="store_true",
                    help="also rewrite authelia/managed/ (rules, users, OIDC) from the profile")
    args = ap.parse_args()
    try:
        {"generate": generate, "bootstrap": bootstrap, "check": check}[args.command](args)
    except (ProfileError, RuntimeError, ValueError) as e:
        sys.exit(f"\nsetup: {e}")


if __name__ == "__main__":
    main()
