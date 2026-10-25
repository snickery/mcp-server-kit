"""Run a FastMCP server over stdio or bearer-gated Streamable HTTP."""

import sys

from mcp.server.fastmcp import FastMCP

from mcp_server_kit.auth import BearerAuthMiddleware


def serve(mcp: FastMCP, *, port: int, bearer_token: str, host: str = "0.0.0.0",
          argv: list[str] | None = None) -> None:
    """`--stdio` in argv runs stdio (the client owns the process, so no auth). Otherwise
    Streamable HTTP at /mcp behind BearerAuthMiddleware, and it refuses to start without
    a token rather than serve unauthenticated.

    Build the FastMCP with stateless_http=True, json_response=True, so a dropped
    connection or a restart never strands a client session.
    """
    argv = sys.argv[1:] if argv is None else argv
    if "--stdio" in argv:
        mcp.run(transport="stdio")
        return
    if not bearer_token:
        raise SystemExit(
            "MCP_BEARER_TOKEN is not set. Refusing to start an unauthenticated "
            "MCP server. Set it in .secrets.env and restart."
        )
    import uvicorn

    app = BearerAuthMiddleware(mcp.streamable_http_app(), expected_token=bearer_token)
    uvicorn.run(app, host=host, port=port)
