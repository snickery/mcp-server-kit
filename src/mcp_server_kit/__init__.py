"""Shared plumbing for FastMCP servers: bearer auth, off-loop tools, serving, healthcheck,
collision-safe file staging (`files`) and PDF reading (`pdf`, extra `[pdf]`)."""

from mcp_server_kit.auth import BearerAuthMiddleware
from mcp_server_kit.serve import serve
from mcp_server_kit.tools import offload_sync_tools, request_logger, rss_mib

__all__ = ["BearerAuthMiddleware", "offload_sync_tools", "request_logger", "rss_mib", "serve"]
__version__ = "0.1.0"
