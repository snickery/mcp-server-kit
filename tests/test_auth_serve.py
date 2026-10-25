import asyncio
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from mcp.server.fastmcp import FastMCP

from mcp_server_kit import BearerAuthMiddleware, serve
from mcp_server_kit.healthcheck import probe


async def _inner(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _call(app, path="/mcp", auth=None, client=("10.0.0.1", 1), kind="http"):
    headers = [(b"host", b"t")] + ([(b"authorization", auth.encode())] if auth else [])
    scope = {"type": kind, "method": "POST", "path": path, "headers": headers, "client": client}
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    return sent


APP = BearerAuthMiddleware(_inner, "secret")


@pytest.mark.parametrize("auth,status", [
    (None, 401), ("Bearer wrong", 401), ("Bearer secret", 200), ("bearer secret", 401),
    ("Basic secret", 401), ("Bearer secretx", 401), ("Bearer ", 401),
])
def test_bearer_check(auth, status):
    assert _call(APP, auth=auth)[0]["status"] == status


def test_rejection_is_json_401():
    sent = _call(APP)
    assert json.loads(sent[1]["body"]) == {"error": "unauthorized"}
    assert [b"content-type", b"application/json"] in sent[0]["headers"]


@pytest.mark.parametrize("path,status", [
    ("/.well-known/oauth-protected-resource", 200),
    ("/mcp/.well-known/x", 401),            # substring is not an exemption
    ("/.well-knownx", 401),
    ("/x/../.well-known/", 401),
])
def test_well_known_exemption_is_prefix_only(path, status):
    assert _call(APP, path=path)[0]["status"] == status


def test_non_http_scopes_pass_through():
    seen = []

    async def lifespan_app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(BearerAuthMiddleware(lifespan_app, "s")({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_empty_token_is_refused():
    with pytest.raises(ValueError):
        BearerAuthMiddleware(_inner, "")


def test_logging_quiet_for_probes_loud_for_bad_tokens(caplog):
    with caplog.at_level(logging.DEBUG, logger="mcp_server_kit.auth"):
        _call(APP, client=("127.0.0.1", 5))                       # probe, no creds
        _call(APP, client=("127.0.0.1", 5), auth="Bearer wrong")  # loopback with a bad token
        _call(APP, client=("10.0.0.9", 5))
    levels = [r.levelno for r in caplog.records]
    assert levels == [logging.DEBUG, logging.WARNING, logging.WARNING]
    assert all("secret" not in r.getMessage() and "wrong" not in r.getMessage() for r in caplog.records)


def test_serve_refuses_http_without_token():
    with pytest.raises(SystemExit, match="Refusing to start"):
        serve(FastMCP("t"), port=1, bearer_token="", argv=[])


def test_serve_stdio_needs_no_token(monkeypatch):
    mcp = FastMCP("t")
    calls = []
    monkeypatch.setattr(mcp, "run", lambda transport: calls.append(transport))
    serve(mcp, port=1, bearer_token="", argv=["--stdio"])
    assert calls == ["stdio"]


def test_serve_http_wraps_the_app_in_the_bearer_gate(monkeypatch):
    import uvicorn
    got = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: got.update(app=app, host=host, port=port))
    serve(FastMCP("t", stateless_http=True, json_response=True), port=8999, bearer_token="tok", argv=[])
    assert isinstance(got["app"], BearerAuthMiddleware) and got["port"] == 8999 and got["host"] == "0.0.0.0"


class _Always401(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(401)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_probe_counts_401_as_alive():
    srv = HTTPServer(("127.0.0.1", 0), _Always401)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        probe(srv.server_address[1])  # returns, no SystemExit
    finally:
        srv.shutdown()


def test_probe_fails_when_nothing_listens():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(SystemExit) as e:
        probe(port, timeout=1)
    assert e.value.code == 1
