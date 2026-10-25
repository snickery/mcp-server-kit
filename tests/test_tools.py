import asyncio
import contextlib
import importlib.util
import logging
import re
import sys
import textwrap
import threading
import time

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from mcp_server_kit import offload_sync_tools

# The audit collector's patterns (claude-mcp-guard mcp-audit-hook): the line format is a contract.
TOOL_RE = re.compile(r"tool=(\S+) outcome=(\S+)(?: duration_ms=(\d+))?")
TEXT_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


@contextlib.contextmanager
def _lines(logger_name):
    """Collect what the kit's per-call logger emits. It doesn't propagate (it has its own
    stderr handler), so attach to it directly rather than relying on caplog."""
    got = []
    handler = logging.Handler()
    handler.emit = lambda record: got.append(record.getMessage())
    log = logging.getLogger(logger_name)
    log.addHandler(handler)
    try:
        yield got
    finally:
        log.removeHandler(handler)


def _define(mcp):
    @mcp.tool()
    def lookup(query: str, limit: int = 5, tags: list[str] | None = None) -> str:
        """Look something up."""
        return f"{query}:{limit}:{tags}"

    @mcp.tool(name="renamed_tool")
    def original_name(x: int) -> int:
        """Doubles."""
        return x * 2

    @mcp.tool()
    async def fetch(url: str) -> str:
        """Async tool."""
        return url

    return lookup


def _schemas(mcp):
    return {t.name: (t.description, t.inputSchema, t.annotations)
            for t in asyncio.run(mcp.list_tools())}


@pytest.fixture
def kit_mcp():
    mcp = FastMCP("t")
    offload_sync_tools(mcp, logger_name="t.requests")
    return mcp


def test_schemas_identical_to_plain_fastmcp(kit_mcp):
    plain = FastMCP("t")
    _define(plain)
    _define(kit_mcp)
    assert _schemas(kit_mcp) == _schemas(plain)


def test_decorator_returns_the_original_sync_function(kit_mcp):
    lookup = _define(kit_mcp)
    assert lookup("a", 2) == "a:2:None"
    assert not asyncio.iscoroutinefunction(lookup)


def test_sync_tool_runs_off_the_event_loop():
    mcp = FastMCP("t")
    offload_sync_tools(mcp, logger_name="t.requests")
    seen = {}

    @mcp.tool()
    def slow(seconds: float) -> str:
        """Blocks."""
        seen["slow_thread"] = threading.get_ident()
        time.sleep(seconds)
        return "slow"

    @mcp.tool()
    async def quick(x: int) -> int:
        """Doesn't block."""
        return x

    async def run():
        seen["loop_thread"] = threading.get_ident()
        order = []

        async def call(name, args):
            await mcp.call_tool(name, args)
            order.append(name)

        await asyncio.gather(call("slow", {"seconds": 0.5}), call("quick", {"x": 1}))
        return order

    order = asyncio.run(run())
    assert order == ["quick", "slow"], "the quick call waited for the blocking one"
    assert seen["slow_thread"] != seen["loop_thread"]


def test_one_log_line_per_call_in_the_audited_format(kit_mcp):
    _define(kit_mcp)
    with _lines("t.requests") as msgs:
        asyncio.run(kit_mcp.call_tool("lookup", {"query": "q"}))
        asyncio.run(kit_mcp.call_tool("renamed_tool", {"x": 2}))
        asyncio.run(kit_mcp.call_tool("fetch", {"url": "u"}))
    assert [TOOL_RE.search(m).group(1, 2) for m in msgs] == [
        ("lookup", "ok"), ("renamed_tool", "ok"), ("fetch", "ok")]
    assert all(re.search(r"duration_ms=\d+ rss_mib=-?\d+$", m) for m in msgs)


def test_handler_format_has_the_timestamp_the_collector_reads():
    from mcp_server_kit.tools import request_logger
    handler = request_logger("t.fmt").handlers[0]
    rec = logging.LogRecord("t.fmt", logging.INFO, __file__, 1, "tool=x outcome=ok duration_ms=1", (), None)
    line = handler.format(rec)
    assert TEXT_TS_RE.match(line) and TOOL_RE.search(line)


def test_errors_are_logged_and_still_raised():
    mcp = FastMCP("t")
    offload_sync_tools(mcp, logger_name="t.err")

    @mcp.tool()
    def boom(x: int) -> int:
        """Fails."""
        raise RuntimeError("nope")

    with _lines("t.err") as msgs, pytest.raises(Exception, match="nope"):
        asyncio.run(mcp.call_tool("boom", {"x": 1}))
    assert len(msgs) == 1 and "tool=boom outcome=error" in msgs[0]


def test_default_annotations_apply_unless_a_tool_sets_its_own():
    ro = ToolAnnotations(readOnlyHint=True)
    rw = ToolAnnotations(destructiveHint=True)
    mcp = FastMCP("t")
    offload_sync_tools(mcp, logger_name="t.ann", default_annotations=ro)

    @mcp.tool()
    def reader(x: int) -> int:
        """r"""
        return x

    @mcp.tool(annotations=rw)
    def writer(x: int) -> int:
        """w"""
        return x

    s = _schemas(mcp)
    assert s["reader"][2] == ro and s["writer"][2] == rw


def test_future_annotations_resolve(tmp_path, monkeypatch):
    """`from __future__ import annotations` + Optional must not break schema generation."""
    mod = tmp_path / "futmod.py"
    mod.write_text(textwrap.dedent('''
        from __future__ import annotations
        from typing import Optional
        from mcp.server.fastmcp import FastMCP
        from mcp_server_kit import offload_sync_tools

        mcp = FastMCP("fut")
        offload_sync_tools(mcp, logger_name="fut.requests")

        @mcp.tool()
        def find(name: Optional[str] = None, limit: int = 3) -> str:
            """Find."""
            return f"{name}:{limit}"
    '''))
    spec = importlib.util.spec_from_file_location("futmod", mod)
    m = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "futmod", m)
    spec.loader.exec_module(m)
    props = _schemas(m.mcp)["find"][1]["properties"]
    assert set(props) == {"name", "limit"} and props["limit"]["default"] == 3
    out = asyncio.run(m.mcp.call_tool("find", {"name": "a"}))
    assert "a:3" in str(out)
