"""MCP 客户端基座（§3.7 / §7）：真实 MCP 协议接入 + 统一容错降级。

- stdio：社区 12306-MCP（npx 拉起 Node 子进程，Node >= 18）
- HTTP/SSE：高德官方 MCP（https://mcp.amap.com/sse?key=KEY，需「Web 服务」Key）
工具名在运行时经 list_tools 发现并按关键词匹配，避免硬编码失效。
"""

from __future__ import annotations

import asyncio
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
from .resilience import ServiceUnavailable, cancel_with_grace


def _direct_client_factory(**kwargs) -> httpx2.AsyncClient:
    """MCP HTTP 客户端工厂：端点均为国内服务，trust_env=False 直连。

    实测（2026-09-03）：VPN 系统代理模式下 httpx2 默认 trust_env=True 会把
    高德/Dida 连接走本地代理，间歇性 ConnectError/TLS 失败；直连则稳定。
    超时用 setdefault 补缺省：mcp SDK 调工厂时若已传入 timeout 不得覆盖
    （SSE 长读超时被缩短会杀掉长连接）。"""
    kwargs["trust_env"] = False
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("timeout", httpx2.Timeout(McpConfig.TIMEOUT_S, read=MCP_DEFAULT_SSE_READ_TIMEOUT))
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
        """按关键词在 list_tools 里找匹配工具并调用；找不到/失败抛 ServiceUnavailable。

        整个生命周期（连接建立 + initialize + list_tools + 调用 + 清理）受
        McpConfig.CALL_TIMEOUT_S 硬上限保护，且超时后"取消 → 0.5s 宽限 → 抛弃"：
        实测（2026-09-04 事故）MCP 传输 wedged 后 anyio 清理阶段会自行挂住，
        wait_for 的超时要等任务真正退出才返回，导致上层超时永不生效、collect 阶段
        整体停摆 26-47 分钟。抛弃 = 不再等待清理完成（登记引用直至任务真正结束）。
        重试职责在调用方（外层 with_retry，每次重试开全新会话）——不在死会话上重试。"""
        task = asyncio.get_running_loop().create_task(self._call_impl(keywords, args, what))
        try:
            _, pending = await asyncio.wait({task}, timeout=McpConfig.CALL_TIMEOUT_S)
        except asyncio.CancelledError:
            # 外部真实取消（用户停止/阶段取消）：取消内部任务后透传
            await cancel_with_grace(task)
            raise
        if pending:
            await cancel_with_grace(task)
            raise ServiceUnavailable(
                f"{what}：MCP 调用超过 {int(McpConfig.CALL_TIMEOUT_S)}s 未完成（疑似连接挂死，已抛弃）")
        if task.cancelled():
            raise ServiceUnavailable(f"{what}：MCP 调用被取消")
        exc = task.exception()
        if exc is not None:
            raise exc
        return task.result()

    async def _call_impl(self, keywords: tuple[str, ...], args: dict[str, Any], what: str) -> Any:
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
            result = await session.call_tool(matched.name, call_args)
            return _extract_content(result)
        finally:
            await stack.aclose()


def _extract_content(result: Any) -> Any:
    """MCP CallToolResult → 纯数据（text JSON 优先）。

    isError=True 时错误文本此前被包成 {"text": ...} 当正常数据返回，上层解析不出
    候选后只报"返回为空"——真实报错被吞。改为抛 ServiceUnavailable，进既有逐通道
    降级链（notice 带原始错误）。"""
    if getattr(result, "isError", False):
        err_texts = [getattr(c, "text", "") for c in (getattr(result, "content", None) or [])
                     if getattr(c, "type", "") == "text"]
        raise ServiceUnavailable("MCP 工具执行报错：" + ("；".join(t for t in err_texts if t) or "（无错误详情）")[:500])
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
