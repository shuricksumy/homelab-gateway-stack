#!/bin/sh
# Homelab gateway setup: gateway.toml → ../deploy, ready for `docker compose up -d`.
#
#   ./setup.sh                 generate (safe to run again after editing gateway.toml)
#   ./setup.sh check           validate the generated files (compose, Authelia)
#   ./setup.sh bootstrap       after `docker compose up -d`: users, VPN server, tunnel keys
#   ./setup.sh --help          every option
#
# Needs Docker. Uses the host's Python when it is 3.11 or newer; otherwise runs setup.py in a
# small container with Python and the Docker CLI (same paths, same Docker).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
root=$(dirname "$here")

if command -v python3 >/dev/null 2>&1 &&
	python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
	exec python3 "$here/setup.py" "$@"
fi

echo "setup: Python 3.11+ not found, running setup.py in a container" >&2
exec docker run --rm -i \
	-v /var/run/docker.sock:/var/run/docker.sock \
	-v "$root:$root" -w "$here" \
	-u 0 \
	docker:28-cli sh -c 'apk add --no-cache python3 >/dev/null && exec python3 setup.py "$@"' setup "$@"
