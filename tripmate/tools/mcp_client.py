"""MCP 客户端基座（§3.7 / §7）：真实 MCP 协议接入 + 统一容错降级。

- stdio：社区 12306-MCP（npx 拉起 Node 子进程，Node >= 18）
- HTTP/SSE：高德官方 MCP（https://mcp.amap.com/sse?key=KEY，需「Web 服务」Key）
工具名在运行时经 list_tools 发现并按关键词匹配，避免硬编码失效。
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

from ..config import McpConfig
from .resilience import ServiceUnavailable, cancel_with_grace, register_abandoned

logger = logging.getLogger("tripmate.tools.mcp_client")


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
            matched = _match_tool(tools, keywords, what)
            call_args = _filter_args(matched, args)
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


# ---------------------------------------------------------------------------
# 阶段级持久会话池（2026-09-05 阶段二）：同一通道的多次调用复用一次握手。
# 此前 McpSession.call 每次都建连+initialize+list_tools+清理——酒店查询里光 AMAP
# 距离就是逐酒店全程握手（1+N 次），是收集阶段时延的最大单项。
# 生命周期锚定 TeamRunner._run_phase（begin 于阶段开始 / end 于 finally），工具函数
# （train_session 等）签名零改动：池未启用时自动回退原短会话路径。
# ---------------------------------------------------------------------------

_ACTIVE_POOL: dict[str, "PersistentMcpSession"] | None = None


def begin_mcp_pool() -> None:
    """阶段开始：启用持久会话池（随后各通道首次调用时建立会话）。"""
    global _ACTIVE_POOL
    _ACTIVE_POOL = {}


async def end_mcp_pool() -> None:
    """阶段结束：关闭全部持久会话（幂等；须在 finally 中调用）。"""
    global _ACTIVE_POOL
    pool, _ACTIVE_POOL = _ACTIVE_POOL, None
    for s in (pool or {}).values():
        try:
            close_task = asyncio.ensure_future(s.close())
            await asyncio.wait_for(asyncio.shield(close_task), timeout=10.0)
        except asyncio.TimeoutError:
            register_abandoned(close_task)
            logger.warning("持久 MCP 会话 %s 关闭超时（10s），已抛弃清理任务", s.name)
        except Exception as e:  # noqa: BLE001 — 单会话关闭失败不影响其他通道
            logger.warning("持久 MCP 会话 %s 关闭异常（%s: %s）", s.name, type(e).__name__, e)


def _pool_get(key: str) -> "PersistentMcpSession | None":
    return _ACTIVE_POOL.get(key) if _ACTIVE_POOL is not None else None


def _pool_put(key: str, session: "PersistentMcpSession") -> None:
    if _ACTIVE_POOL is not None:
        _ACTIVE_POOL[key] = session


def _match_tool(tools, keywords: tuple[str, ...], what: str):
    """按关键词在工具列表中匹配（子串全命中）；找不到抛 ServiceUnavailable。"""
    matched = None
    for t in tools:
        name = t.name.lower()
        if all(k in name for k in keywords):
            matched = t
            break
    if matched is None:
        raise ServiceUnavailable(f"{what}：MCP 工具列表中未找到匹配 {keywords} 的工具")
    return matched


def _filter_args(matched, args: dict[str, Any]) -> dict[str, Any]:
    """只传该工具 schema 声明的顶层参数（社区服务参数名不一致时尽量兼容）。"""
    schema_props = ((getattr(matched, "input_schema", None) or {}).get("properties", {})) \
        if not isinstance(getattr(matched, "input_schema", None), dict) \
        else (matched.input_schema.get("properties", {}) or {})
    call_args = args if not schema_props else {k: v for k, v in args.items() if k in schema_props}
    if not call_args and schema_props:
        call_args = args  # schema 不透明时原样透传
    return call_args


class PersistentMcpSession:
    """阶段级持久会话：建连 + initialize + list_tools 一次，跨多次 call 复用。

    - 单次调用受 McpConfig.CALL_TIMEOUT_S 硬上限（纯调用，不含握手）；
      握手独立受 HANDSHAKE_TIMEOUT_S 保护（npx 冷启动不挤占调用预算）。
    - 调用超时/失败 → 重置会话引用（下次调用重新握手；外层 with_retry 负责重试）；
      被抛弃任务的清理继续在后台执行（mcp SDK 自带进程树终止）。
    - 同会话调用按构造即串行（车票/酒店/天气各自独立会话），不加锁避免
      "抛弃的挂死调用永久持锁"死锁。
    - actor 模式（2026-09-05 e2e 实测修复）：传输栈由专属 supervisor 任务持有——
      栈在该任务内进入与退出。此前栈在 JobBoard 任务内进入、在 end_mcp_pool 里退出，
      触发 anyio "cancel scope in a different task" 异常，干净关闭失败、stdio 子进程
      残留。close() 只向 supervisor 发停止信号并在其任务内完成 aclose。"""

    def __init__(self, transport: str, command: str = "", url: str = "",
                 headers: dict[str, str] | None = None, name: str = "") -> None:
        self._transport = transport
        self._command = command
        self._url = url
        self._headers = headers or {}
        self.name = name or transport
        self._stack: AsyncExitStack | None = None
        self._session = None
        self._tools = None
        self._inflight: asyncio.Task | None = None
        self._supervisor: asyncio.Task | None = None
        self._ready: asyncio.Event | None = None
        self._stop: asyncio.Event | None = None
        self._open_error: BaseException | None = None

    async def _supervise_open(self) -> None:
        """专属持有任务：栈在本任务内进入；收到停止信号后在同任务内 aclose。"""
        stack = AsyncExitStack()
        self._stack = stack
        try:
            if self._transport == "stdio":
                parts = self._command.split()
                params = StdioServerParameters(command=parts[0], args=parts[1:])
                read, write = await stack.enter_async_context(stdio_client(params))
            elif self._transport == "sse":
                read, write = await stack.enter_async_context(
                    sse_client(self._url, headers=self._headers or None,
                               httpx_client_factory=_direct_client_factory))
            else:
                http_client = await stack.enter_async_context(
                    _direct_client_factory(
                        headers=self._headers or None,
                        timeout=httpx2.Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT)))
                read, write = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http_client))
            session = ClientSession(read, write)
            await stack.enter_async_context(session)
            await session.initialize()
            self._tools = (await session.list_tools()).tools
            self._session = session
        except Exception as e:  # noqa: BLE001 — 开启失败：记录并唤醒等待者
            self._open_error = e
            self._stack = None
            try:
                await stack.aclose()
            except Exception:  # noqa: BLE001
                pass
        finally:
            if self._ready is not None:
                self._ready.set()
        if self._session is None:
            return
        # 保持存活直至 close/_reset 请求停止；aclose 在"本任务"内执行（亲和性正确）
        await self._stop.wait()
        self._session = None
        self._tools = None
        self._stack = None
        try:
            await stack.aclose()
        except Exception as e:  # noqa: BLE001 — 关闭失败仅记日志（mcp 清理含进程树终止）
            logger.warning("持久 MCP 会话 %s 关闭异常（%s: %s）", self.name, type(e).__name__, e)

    async def _ensure_open(self) -> None:
        if self._session is not None:
            return
        self._open_error = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._supervisor = asyncio.create_task(self._supervise_open())
        await self._ready.wait()
        if self._open_error is not None:
            self._supervisor = None
            raise self._open_error

    def _reset(self) -> None:
        """失败/超时后重置：请求 supervisor 退出（栈在其自身任务内干净关闭），
        下次调用重新握手。不等待退出（被抛弃任务的清理继续在后台执行）。"""
        self._session = None
        self._tools = None
        self._stack = None
        self._supervisor = None
        if self._stop is not None:
            self._stop.set()

    async def close(self) -> None:
        """阶段结束关闭：先宽限终止在途调用，再通知 supervisor 在其任务内关闭传输（幂等）。"""
        inflight, self._inflight = self._inflight, None
        if inflight is not None and not inflight.done():
            await cancel_with_grace(inflight)
        sup, self._supervisor = self._supervisor, None
        self._session = None
        self._tools = None
        self._stack = None
        if sup is None or sup.done():
            return
        if self._stop is not None:
            self._stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(sup), timeout=10.0)
        except asyncio.TimeoutError:
            register_abandoned(sup)
            logger.warning("持久 MCP 会话 %s 关闭超时（10s），已抛弃清理任务", self.name)
        except asyncio.CancelledError:
            register_abandoned(sup)
            raise
        except Exception as e:  # noqa: BLE001 — 单会话关闭失败不影响其他通道
            logger.warning("持久 MCP 会话 %s 关闭异常（%s: %s）", self.name, type(e).__name__, e)

    async def call(self, keywords: tuple[str, ...], args: dict[str, Any], what: str) -> Any:
        """复用会话调用工具；找不到/失败抛 ServiceUnavailable。接口与 McpSession.call 一致。"""
        # 握手独立计时（npx 冷启动可达数十秒，不挤占单次调用 90s 预算）
        hs_task = asyncio.get_running_loop().create_task(self._ensure_open())
        self._inflight = hs_task
        try:
            try:
                await asyncio.wait_for(asyncio.shield(hs_task),
                                       timeout=McpConfig.HANDSHAKE_TIMEOUT_S)
            except asyncio.TimeoutError:
                await cancel_with_grace(hs_task)
                self._reset()
                raise ServiceUnavailable(
                    f"{what}：MCP 握手超过 {int(McpConfig.HANDSHAKE_TIMEOUT_S)}s 未完成（已抛弃）")
            except asyncio.CancelledError:
                await cancel_with_grace(hs_task)
                self._reset()
                raise

            call_task = asyncio.get_running_loop().create_task(self._call_opened(keywords, args, what))
            self._inflight = call_task
            try:
                _, pending = await asyncio.wait({call_task}, timeout=McpConfig.CALL_TIMEOUT_S)
            except asyncio.CancelledError:
                await cancel_with_grace(call_task)
                self._reset()
                raise
            if pending:
                await cancel_with_grace(call_task)
                self._reset()
                raise ServiceUnavailable(
                    f"{what}：MCP 调用超过 {int(McpConfig.CALL_TIMEOUT_S)}s 未完成（疑似挂死，已抛弃并重置会话）")
            exc = call_task.exception()
            if exc is not None:
                self._reset()  # 失败后重置：下次调用重新握手（重试由外层 with_retry 负责）
                raise exc
            return call_task.result()
        finally:
            self._inflight = None

    async def _call_opened(self, keywords: tuple[str, ...], args: dict[str, Any], what: str) -> Any:
        matched = _match_tool(self._tools, keywords, what)
        call_args = _filter_args(matched, args)
        result = await self._session.call_tool(matched.name, call_args)
        return _extract_content(result)


# ---------------------------------------------------------------------------
# 通道工厂（接口不变；池启用时返回/创建对应持久会话，否则维持原短会话行为）
# ---------------------------------------------------------------------------

def amap_session() -> McpSession | PersistentMcpSession:
    """高德官方 MCP（SSE 通道，Key 在 URL 上）。未配置 Key 时构造也允许，调用前由上层判断。"""
    if not McpConfig.AMAP_API_KEY:
        raise ServiceUnavailable("未配置 AMAP_API_KEY（高德官方 MCP 需要「Web 服务」Key）")
    url = f"{McpConfig.AMAP_MCP_URL}?key={McpConfig.AMAP_API_KEY}"
    transport = "sse" if "/sse" in McpConfig.AMAP_MCP_URL else "http"
    if _ACTIVE_POOL is None:
        return McpSession(transport, url=url)
    pooled = _pool_get("amap")
    if pooled is not None:
        return pooled
    s = PersistentMcpSession(transport, url=url, name="amap")
    _pool_put("amap", s)
    return s


def train_session() -> McpSession | PersistentMcpSession:
    """社区 12306-MCP（stdio：npx 拉起 Node 子进程）。"""
    if not McpConfig.MCP_12306_COMMAND:
        raise ServiceUnavailable("未配置 MCP_12306_COMMAND（社区 12306-MCP）")
    if _ACTIVE_POOL is None:
        return McpSession("stdio", command=McpConfig.MCP_12306_COMMAND)
    pooled = _pool_get("12306")
    if pooled is not None:
        return pooled
    s = PersistentMcpSession("stdio", command=McpConfig.MCP_12306_COMMAND, name="12306")
    _pool_put("12306", s)
    return s


def hotel_session() -> McpSession | PersistentMcpSession:
    """Dida 酒店 MCP（Streamable HTTP，Bearer Token 鉴权）。"""
    if not McpConfig.MCP_HOTEL_URL:
        raise ServiceUnavailable("未配置 MCP_HOTEL_URL（Dida 酒店 MCP）")
    headers = {"Authorization": f"Bearer {McpConfig.MCP_HOTEL_TOKEN}"} if McpConfig.MCP_HOTEL_TOKEN else None
    transport = "sse" if "/sse" in McpConfig.MCP_HOTEL_URL else "http"
    if _ACTIVE_POOL is None:
        return McpSession(transport, url=McpConfig.MCP_HOTEL_URL, headers=headers)
    pooled = _pool_get("hotel")
    if pooled is not None:
        return pooled
    s = PersistentMcpSession(transport, url=McpConfig.MCP_HOTEL_URL, headers=headers, name="hotel")
    _pool_put("hotel", s)
    return s
