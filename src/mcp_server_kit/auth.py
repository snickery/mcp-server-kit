"""Bearer-token gate for a Streamable HTTP MCP app."""

import hmac
import json
import logging

logger = logging.getLogger(__name__)

_LOOPBACK = ("127.0.0.1", "::1")


class BearerAuthMiddleware:
    """Pure ASGI bearer-token middleware (streaming-safe, timing-safe compare).

    Every HTTP/websocket request needs `Authorization: Bearer <expected_token>`,
    except `/.well-known/` discovery paths. That exemption is a prefix match on
    purpose: a substring match would become an auth bypass the moment the app
    routes anything under /mcp. Rejections get a JSON 401. A loopback request
    with no credentials (a liveness probe) is logged at debug; anything else is
    a warning. The token itself is never logged.
    """

    def __init__(self, app, expected_token: str):
        if not expected_token:
            raise ValueError("BearerAuthMiddleware needs a non-empty token")
        self.app = app
        self.expected_token = expected_token

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if scope.get("path", "").startswith("/.well-known/"):
            return await self.app(scope, receive, send)
        auth = dict(scope.get("headers", [])).get(b"authorization", b"").decode()
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], self.expected_token):
            return await self.app(scope, receive, send)

        client_host = (scope.get("client") or ("unknown",))[0]
        if client_host in _LOOPBACK and not auth:
            logger.debug("Health probe 401 (loopback, no credentials, expected)")
        else:
            logger.warning("Rejected request: invalid or missing bearer token from %s", client_host)
        body = json.dumps({"error": "unauthorized"}).encode()
        await send({"type": "http.response.start", "status": 401, "headers": [
            [b"content-type", b"application/json"],
            [b"content-length", str(len(body)).encode()],
        ]})
        await send({"type": "http.response.body", "body": body})
