"""LangChain / LangGraph integration with the ZeroGraph MCP server."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.interceptors import MCPToolCallRequest, MCPToolCallResult
from langchain_mcp_adapters.sessions import Connection, create_session
from langchain_mcp_adapters.tools import load_mcp_tools

logger = logging.getLogger(__name__)


def mcp_http_connection(url: str) -> Connection:
    """Build a streamable-HTTP MCP connection config for *url*."""
    return {"transport": "http", "url": url}


class ZeroGraphToolInterceptor:
    """Inject index defaults and log tool calls."""

    def __init__(
        self,
        *,
        source_type: str,
        source_path: str,
        language: str,
        github_token: str | None,
    ) -> None:
        self.source_type = source_type
        self.source_path = source_path
        self.language = language
        self.github_token = github_token

    async def __call__(
        self,
        request: MCPToolCallRequest,
        handler: Any,
    ) -> MCPToolCallResult:
        if request.name == "zg_index_repo":
            args = dict(request.args)
            args.setdefault("source_type", self.source_type)
            args.setdefault("source_path", self.source_path)
            args.setdefault("language", self.language)
            if self.github_token:
                args.setdefault("github_token", self.github_token)
            request = request.override(args=args)
        logger.info("MCP tool: %s(%s)", request.name, list(request.args.keys()))
        return await handler(request)


@asynccontextmanager
async def zerograph_tools(
    mcp_url: str,
    *,
    source_type: str,
    source_path: str,
    language: str,
    github_token: str | None,
) -> AsyncIterator[list[BaseTool]]:
    """Yield LangChain tools bound to a live ZeroGraph MCP session."""
    connection = mcp_http_connection(mcp_url)
    async with create_session(connection) as session:
        await session.initialize()
        interceptor = ZeroGraphToolInterceptor(
            source_type=source_type,
            source_path=source_path,
            language=language,
            github_token=github_token,
        )
        tools = await load_mcp_tools(
            session,
            connection=connection,
            tool_interceptors=[interceptor],
        )
        zg_tools = [t for t in tools if t.name.startswith("zg_")]
        logger.info("Loaded %d LangChain tools (%d zg_*)", len(tools), len(zg_tools))
        yield zg_tools
