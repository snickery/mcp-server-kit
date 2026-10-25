# mcp-server-kit

Shared plumbing for Python MCP servers built on the official SDK's FastMCP
(`mcp.server.fastmcp`). It covers the parts every server ends up copying:

| Module | What it does |
|---|---|
| `offload_sync_tools` | Registers each **sync** tool as an async wrapper that runs the body in a worker thread, so one blocking upstream call can't freeze every other request. The SDK runs sync handlers inline on the event loop. Signatures, and so input schemas, are unchanged. Every call, sync or async, logs one `tool=… outcome=… duration_ms=… rss_mib=…` line. |
| `BearerAuthMiddleware` | Pure ASGI bearer-token gate (timing-safe compare, streaming-safe). Only `/.well-known/` discovery paths are exempt, matched by prefix, never by substring. |
| `serve` | `--stdio` runs stdio. Otherwise it serves Streamable HTTP at `/mcp` behind the bearer gate, and **refuses to start without a token**. |
| `healthcheck.probe` | A liveness probe for containers: a full HTTP round-trip to `/mcp`, where the 401 counts as alive. Pair it with restart-on-unhealthy so a wedged loop heals itself. |
| `files` | Collision-safe staging for a directory several servers share: bare names only, never overwrite (`-2`, `-3`, … before the extension, `O_CREAT\|O_EXCL`), `sha256` of every transfer. `atomic_create` writes bytes; `exclusive_open` hands a streaming writer the fd. `list_files` / `purge_one` / `purge_older` (0.1.1) are the bodies of the matching housekeeping tools: listing with sha256 and `<scheme>:<name>` references, delete by bare name, age-based purge with dry run. |
| `pdf` (extra `[pdf]`) | PDF text extraction (pdfplumber text / layout / tables), page rendering (pypdfium2), and OCR through an [xberg](https://github.com/xberg-io/xberg) server (tesseract, or a vision model for hard pages). Configured by `XBERG_*` env vars; the PDF libraries load lazily. |

## Install

```bash
uv add mcp-server-kit        # or: pip install mcp-server-kit
```

It pins `mcp>=1,<2`, so the SDK range lives in one place for every server built on the kit.

## Use

```python
import logging
import os

from mcp.server.fastmcp import FastMCP
from mcp_server_kit import offload_sync_tools, serve

mcp = FastMCP("example", instructions="...", stateless_http=True, json_response=True)
offload_sync_tools(mcp, logger_name="example_mcp.requests")   # before the first @mcp.tool()


@mcp.tool()
def lookup(query: str) -> str:
    """A blocking call is fine here: it runs in a worker thread."""
    ...


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    serve(mcp, port=8000, bearer_token=os.environ.get("MCP_BEARER_TOKEN", ""))
```

`lookup` stays a plain function, so tests and other tools can call it directly.

To give every tool the same annotations unless it passes its own:
`offload_sync_tools(mcp, logger_name=..., default_annotations=ToolAnnotations(readOnlyHint=True))`.

Healthcheck module for a container (`HealthCmd=python -m example_mcp.healthcheck`):

```python
from mcp_server_kit.healthcheck import probe

def main() -> None:
    probe(8000)

if __name__ == "__main__":
    main()
```

Shared non-thread-safe clients (e.g. cached `httplib2` objects) need a lock in the
server. Thread-safe ones (`httpx.Client`, `requests.Session`) don't.

## Develop

```bash
uv sync --extra test
uv run pytest -q
```

MIT licensed.
