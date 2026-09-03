"""MCP 客户端基座（§3.7 / §7）：真实 MCP 协议接入 + 统一容错降级。

- stdio：社区 12306-MCP（npx 拉起 Node 子进程，Node >= 18）
- HTTP/SSE：高德官方 MCP（https://mcp.amap.com/sse?key=KEY，需「Web 服务」Key）
工具名在运行时经 list_tools 发现并按关键词匹配，避免硬编码失效。
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from ..config import McpConfig
from .resilience import ServiceUnavailable, with_retry


def _direct_client_factory(**kwargs) -> httpx2.AsyncClient:
    """MCP HTTP 客户端工厂：端点均为国内服务，trust_env=False 直连。

    实测（2026-09-03）：VPN 系统代理模式下 httpx2 默认 trust_env=True 会把
    高德/Dida 连接走本地代理，间歇性 ConnectError/TLS 失败；直连则稳定。"""
    kwargs["trust_env"] = False
    kwargs.setdefault("follow_redirects", True)
    return httpx2.AsyncClient(**kwargs)


class McpSession:
    """单次连接内调用若干工具后即关闭（短会话，避免子进程常驻）。"""

    def __init__(self, transport: str, command: str = "", url: str = "",
                 headers: dict[str, str] | None = None) -> None:
        self._transport = transport  # "stdio" | "sse" | "http"
        self._command = command
        self._url = url
        self._headers = headers or {}

    async def _open(self) -> tuple[AsyncExitStack, ClientSession]:
        stack = AsyncExitStack()
        try:
            if self._transport == "stdio":
                parts = self._command.split()
                params = StdioServerParameters(command=parts[0], args=parts[1:])
                read, write = await stack.enter_async_context(stdio_client(params))
            elif self._transport == "sse":
                read, write = await stack.enter_async_context(
                    sse_client(self._url, headers=self._headers or None,
                               httpx_client_factory=_direct_client_factory))
            else:  # streamable http（本版 mcp 只 yield 两值；鉴权头经预配置 http 客户端注入）
                http_client = await stack.enter_async_context(
                    _direct_client_factory(
                        headers=self._headers or None,
                        timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)))
                read, write = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http_client))
            session = ClientSession(read, write)
            await stack.enter_async_context(session)
            await session.initialize()
            return stack, session
        except Exception:
            await stack.aclose()
            raise

    async def call(self, keywords: tuple[str, ...], args: dict[str, Any], what: str) -> Any:
        """按关键词在 list_tools 里找匹配工具并调用；找不到/失败抛 ServiceUnavailable。"""
        stack, session = await self._open()
        try:
            tools = await session.list_tools()
            matched = None
            for t in tools.tools:
                name = t.name.lower()
                if all(k in name for k in keywords):
                    matched = t
                    break
            if matched is None:
                raise ServiceUnavailable(f"{what}：MCP 工具列表中未找到匹配 {keywords} 的工具")
            # 只传该工具 schema 声明的参数（社区服务参数名不一致时尽量兼容）
            schema_props = ((getattr(matched, "input_schema", None) or {}).get("properties", {})) \
                if not isinstance(getattr(matched, "input_schema", None), dict) \
                else (matched.input_schema.get("properties", {}) or {})
            call_args = args if not schema_props else {k: v for k, v in args.items() if k in schema_props}
            if not call_args and schema_props:
                call_args = args  # schema 不透明时原样透传
            result = await with_retry(
                lambda: session.call_tool(matched.name, call_args),
                timeout_s=McpConfig.TIMEOUT_S, retries=McpConfig.RETRIES,
                delay_s=McpConfig.RETRY_DELAY_S, what=what)
            return _extract_content(result)
        finally:
            await stack.aclose()


def _extract_content(result: Any) -> Any:
    """MCP CallToolResult → 纯数据（text JSON 优先）。"""
    content = getattr(result, "content", None) or []
    texts = [getattr(c, "text", "") for c in content if getattr(c, "type", "") == "text"]
    if not texts:
        return {"raw": str(result)[:2000]}
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except (json.JSONDecodeError, TypeError):
        return {"text": joined[:4000]}


def amap_session() -> McpSession:
    """高德官方 MCP（SSE 通道，Key 在 URL 上）。未配置 Key 时构造也允许，调用前由上层判断。"""
    if not McpConfig.AMAP_API_KEY:
        raise ServiceUnavailable("未配置 AMAP_API_KEY（高德官方 MCP 需要「Web 服务」Key）")
    url = f"{McpConfig.AMAP_MCP_URL}?key={McpConfig.AMAP_API_KEY}"
    transport = "sse" if "/sse" in McpConfig.AMAP_MCP_URL else "http"
    return McpSession(transport, url=url)


def train_session() -> McpSession:
    """社区 12306-MCP（stdio：npx 拉起 Node 子进程）。"""
    if not McpConfig.MCP_12306_COMMAND:
        raise ServiceUnavailable("未配置 MCP_12306_COMMAND（社区 12306-MCP）")
    return McpSession("stdio", command=McpConfig.MCP_12306_COMMAND)


def hotel_session() -> McpSession:
    """Dida 酒店 MCP（Streamable HTTP，Bearer Token 鉴权）。"""
    if not McpConfig.MCP_HOTEL_URL:
        raise ServiceUnavailable("未配置 MCP_HOTEL_URL（Dida 酒店 MCP）")
    headers = {"Authorization": f"Bearer {McpConfig.MCP_HOTEL_TOKEN}"} if McpConfig.MCP_HOTEL_TOKEN else None
    transport = "sse" if "/sse" in McpConfig.MCP_HOTEL_URL else "http"
    return McpSession(transport, url=McpConfig.MCP_HOTEL_URL, headers=headers)
