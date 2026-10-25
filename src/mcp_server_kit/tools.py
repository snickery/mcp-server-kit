"""Tool registration: run sync handlers off the event loop, log one line per call.

The MCP SDK runs a synchronous tool handler inline on the single asyncio event
loop, so one blocking upstream call (httpx, httplib2, time.sleep) stalls every
other request until it returns. `offload_sync_tools` rewires `mcp.tool` so each
sync handler is registered as an async wrapper that runs the body in a worker
thread. The tool's signature is preserved, so its input schema is unchanged.
The decorator returns the original function, so module-level names stay
directly callable (tool-to-tool calls, unit tests).

Every call, sync or async, logs one line in the format audit tooling parses:

    <asctime> INFO tool=<name> outcome=ok|error duration_ms=<n> rss_mib=<n>

Guard non-thread-safe shared clients (e.g. cached httplib2 objects) with a lock
in the server; thread-safe ones (httpx.Client, requests.Session) need none.
"""

import functools
import inspect
import logging
import os
import time
from typing import Any

import anyio
from mcp.server.fastmcp import FastMCP

_LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def rss_mib() -> int:
    """Current process RSS in MiB from /proc (Linux), or -1 where that isn't available."""
    try:
        with open("/proc/self/statm") as f:
            pages = int(f.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return -1


def request_logger(name: str) -> logging.Logger:
    """The per-call logger: its own stderr handler, so the line format doesn't depend on
    how (or whether) the server configured root logging. stderr is safe under stdio."""
    log = logging.getLogger(name)
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(_LOG_FORMAT))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


def _signature(fn) -> inspect.Signature:
    # Resolve string annotations (`from __future__ import annotations`): FastMCP/pydantic
    # can't resolve forward refs like Optional[str] through the wrapper indirection.
    try:
        return inspect.signature(fn, eval_str=True)
    except Exception:  # noqa: BLE001 - unresolvable name: fall back to the raw signature
        return inspect.signature(fn)


def offload_sync_tools(mcp: FastMCP, *, logger_name: str, default_annotations: Any = None) -> None:
    """Rewire `mcp.tool` (call once, before the first @mcp.tool()).

    logger_name: the per-call logger, conventionally "<package>.requests".
    default_annotations: ToolAnnotations applied to tools that don't pass their own.
    """
    register_tool = mcp.tool
    log = request_logger(logger_name)

    def tool(*d_args, **d_kwargs):
        if default_annotations is not None:
            d_kwargs.setdefault("annotations", default_annotations)
        register = register_tool(*d_args, **d_kwargs)

        def decorator(fn):
            name = d_kwargs.get("name") or fn.__name__

            def done(start: float, outcome: str) -> None:
                log.info("tool=%s outcome=%s duration_ms=%d rss_mib=%d",
                         name, outcome, int((time.perf_counter() - start) * 1000), rss_mib())

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def wrapper(*args, **kwargs):
                    start, outcome = time.perf_counter(), "ok"
                    try:
                        return await fn(*args, **kwargs)
                    except Exception:
                        outcome = "error"
                        raise
                    finally:
                        done(start, outcome)
            else:
                @functools.wraps(fn)
                async def wrapper(*args, **kwargs):
                    start, outcome = time.perf_counter(), "ok"
                    try:
                        return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
                    except Exception:
                        outcome = "error"
                        raise
                    finally:
                        done(start, outcome)

            sig = _signature(fn)
            wrapper.__signature__ = sig
            wrapper.__annotations__ = {
                **{p.name: p.annotation for p in sig.parameters.values()
                   if p.annotation is not inspect.Parameter.empty},
                **({"return": sig.return_annotation}
                   if sig.return_annotation is not inspect.Signature.empty else {}),
            }
            register(wrapper)
            return fn

        return decorator

    mcp.tool = tool
