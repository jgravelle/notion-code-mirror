"""Thin async context managers for jcodemunch-mcp and Notion MCP stdio servers."""

import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _extract_result(result: Any) -> dict:
    """Extract text content from an MCP tool result and parse as JSON."""
    if hasattr(result, "content") and result.content:
        for item in result.content:
            if hasattr(item, "text"):
                text = item.text
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"raw": text}
    # If result itself is dict-like, return it directly
    if isinstance(result, dict):
        return result
    return {}


class MCPSession:
    """Wraps an MCP ClientSession with a simple async call() interface."""

    def __init__(self, session: ClientSession):
        self._session = session

    async def call(self, tool: str, **kwargs) -> dict:
        """Call an MCP tool by name and return the parsed result."""
        # Filter out None values so we don't send optional args unnecessarily
        args = {k: v for k, v in kwargs.items() if v is not None}
        result = await self._session.call_tool(tool, args)
        return _extract_result(result)

    async def list_tools(self) -> list[str]:
        """Return a list of available tool names on this server."""
        result = await self._session.list_tools()
        return [t.name for t in result.tools]


@asynccontextmanager
async def jcodemunch_session() -> AsyncGenerator[MCPSession, None]:
    """Async context manager that connects to the local jcodemunch-mcp server."""
    cmd = shutil.which("jcodemunch-mcp")
    if not cmd:
        raise RuntimeError(
            "jcodemunch-mcp not found in PATH.\n"
            "Install with: pip install jcodemunch-mcp"
        )

    params = StdioServerParameters(
        command=cmd,
        args=[],
        env={**os.environ},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield MCPSession(session)


@asynccontextmanager
async def notion_mcp_session() -> AsyncGenerator[MCPSession, None]:
    """Async context manager that connects to the Notion MCP server via npx.

    Requires NOTION_API_KEY in the environment and
    @notionhq/notion-mcp-server available via npx.
    """
    notion_key = os.environ.get("NOTION_API_KEY", "")
    if not notion_key:
        raise RuntimeError("NOTION_API_KEY environment variable is not set")

    # Use npx.cmd on Windows, npx elsewhere
    npx = "npx.cmd" if sys.platform == "win32" else "npx"

    params = StdioServerParameters(
        command=npx,
        args=["-y", "@notionhq/notion-mcp-server"],
        env={**os.environ, "NOTION_API_KEY": notion_key},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield MCPSession(session)
