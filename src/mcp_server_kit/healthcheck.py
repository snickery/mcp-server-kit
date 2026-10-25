"""Container liveness probe for a kit-served MCP server.

A full HTTP round-trip to /mcp within a short timeout proves the event loop is
alive. The bearer middleware's 401 to this unauthenticated loopback probe is a
completed response and counts as healthy. A transport failure or timeout exits
non-zero, so a restart-on-unhealthy policy (Podman `HealthOnFailure=kill`)
self-heals a wedged loop. Wire it from the server's own module:

    # <package>/healthcheck.py
    from mcp_server_kit.healthcheck import probe
    from <package>.config import PORT

    def main() -> None:
        probe(PORT)
"""

import sys
import urllib.error
import urllib.request


def probe(port: int, timeout: float = 5) -> None:
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp", timeout=timeout)
    except urllib.error.HTTPError:
        return
    except Exception as exc:  # noqa: BLE001 - any transport failure = unhealthy
        print(f"health: unreachable on :{port}: {exc}", file=sys.stderr)
        sys.exit(1)
