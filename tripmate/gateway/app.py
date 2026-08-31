"""FastAPI 网关（§3.1）：WebSocket 双向通信（聊天输入 + STATUS_* 状态推送）+ 静态资源。

需求 2（2026-08-30）：多会话对话管理——每个会话独立 Session（黑板/团队/聊天），
WS 以 ?sid= 绑定会话，切换会话=换 sid 重连（服务端补播该会话历史）。
内存态：服务重启后会话丢失（MVP 边界，README 已注明）。
"""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import BASE_DIR, OUTPUT_DIR, ServerConfig
from ..llm import usage_summary
from ..planning import compute_budget
from ..session import Session
from ..status import event_json

app = FastAPI(title="TripMate 多 Agent 协同旅游规划系统")

_env = Environment(loader=FileSystemLoader(BASE_DIR / "tripmate" / "templates"),
                   autoescape=select_autoescape(["html"]))

# ---- 会话注册表（需求 2）----
DEFAULT_SID = "default"
sessions: dict[str, Session] = {DEFAULT_SID: Session()}


def _get_session(sid: str | None) -> tuple[str, Session]:
    """按 sid 取会话；未知 sid 视为新会话注册（前端切换/刷新天然幂等）。"""
    key = (sid or "").strip() or DEFAULT_SID
    if key not in sessions:
        sessions[key] = Session()
    return key, sessions[key]


def _session_title(sess: Session) -> str:
    """会话标题由黑板状态派生：目的地 + 生命周期阶段（不持久化，随状态即时变化）。"""
    p = sess.bb.profile
    dest = p.basic_info.destination or "新对话"
    if p.final:
        stage = "已完成"
    elif sess.runner.active:
        stage = "规划中"
    elif p.draft:
        stage = "待确认草稿"
    else:
        stage = "收集需求中"
    return f"{dest} · {stage}"


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/sessions")
async def list_sessions() -> JSONResponse:
    return JSONResponse([{"sid": k, "title": _session_title(v)} for k, v in sessions.items()])


@app.post("/api/sessions")
async def create_session() -> JSONResponse:
    sid = f"s-{uuid.uuid4().hex[:8]}"
    sessions[sid] = Session()
    return JSONResponse({"sid": sid, "title": _session_title(sessions[sid])})


@app.get("/api/profile")
async def profile(sid: str = DEFAULT_SID) -> JSONResponse:
    _, sess = _get_session(sid)
    return JSONResponse(sess.profile_snapshot())


@app.get("/api/usage")
async def usage() -> JSONResponse:
    # 用量是进程级（共享模型客户端），不分会话
    return JSONResponse(usage_summary())


def _render_draft_html(sess: Session) -> str:
    draft = sess.bb.profile.draft
    if not draft:
        return ""
    budget = compute_budget(sess.bb.profile, draft)
    tpl = _env.get_template("draft.html")
    return tpl.render(draft=draft, budget=budget,
                      basic=sess.bb.profile.basic_info,
                      detail=sess.bb.profile.detail_info)


def _orders_payload(sess: Session) -> list[dict]:
    final = sess.bb.profile.final
    return (final.order_summary if final else []) or [
        *[{"type": "车票", "name": t.train_no, "amount": t.price,
           "link": t.link, "reason": t.reason, "selected": t.selected,
           "reference_only": t.reference_only} for t in sess.bb.profile.tickets],
        *[{"type": "酒店", "name": h.name, "amount": h.price_per_night,
           "link": h.link, "reason": h.reason, "selected": h.selected,
           "reference_only": h.reference_only} for h in sess.bb.profile.hotels],
    ]


async def _send(ws: WebSocket, payload: dict, sess: Session | None = None) -> None:
    try:
        await ws.send_text(event_json(payload))
    except Exception as e:  # noqa: BLE001 — 客户端断开由外层统一处理
        from ..status import AUDIT
        AUDIT.output("TeamRunner", f"WS 发送失败（{type(e).__name__}: {e}）：payload type={payload.get('type')}")
        # 丢回复补偿（2026-08-31 实测：回复已生成但 socket 已断 → 用户零收到，而重连补播只含状态
        # 不含聊天）：把未送达的回复降级写入时间线，断线重连后补播可见。仅补偿 chat 非用户回显，
        # 补偿本体发送失败只会被下次 _send 吞掉，无递归。
        if sess and payload.get("type") == "chat" and payload.get("role") != "user":
            try:
                text = str(payload.get("text") or "")[:120]
                await sess.bus.emit("Chatter", f"（可能未送达）{text}", "STATUS_INFO")
            except Exception:  # noqa: BLE001
                pass


async def _push_stream_error(sess: Session, text: str) -> None:
    """向时间线推送一条错误状态（best-effort：失败即丢弃，绝不向上抛）。"""
    try:
        await sess.bus.emit("TeamRunner", text, "STATUS_ERROR")
    except Exception:  # noqa: BLE001
        pass


async def _handle_team_event(ws: WebSocket, sess: Session, item) -> None:
    """团队事件 → 前端卡片 + Chatter 转述。卡片先于转述推送（转述挂了卡片也已送达）。"""
    kind, data = item
    if kind == "draft_ready":
        await _send(ws, {"type": "draft", "html": _render_draft_html(sess),
                         "draft": sess.bb.profile.draft.model_dump(mode="json"),
                         "budget": compute_budget(sess.bb.profile,
                                                  sess.bb.profile.draft)}, sess)
        reply = await sess.relay_team_event(
            "规划团队已产出行程草稿（黑板 draft 分区已就绪）。请读取后向用户转述逐日概要与预算结论，"
            "并询问是否需要修改（用户可提出修改意见或确认）。")
        await _send(ws, {"type": "chat", "role": "chatter", "text": reply}, sess)
    elif kind == "completed":
        await _send(ws, {"type": "final", "pdf_url": data.pdf_url,
                         "orders": data.order_summary,
                         "total_price": data.total_price}, sess)
        await _send(ws, {"type": "usage", "usage": usage_summary()})  # 定稿即刷新消耗条（不等下次聊天）
        reply = await sess.relay_team_event(
            "规划团队已完成定稿（黑板 final 分区：PDF + 推荐订单清单）。请读取后向用户转述成果要点，"
            "提醒逐项确认订单并自行在官方渠道支付。")
        await _send(ws, {"type": "chat", "role": "chatter", "text": reply}, sess)
    elif kind == "error":
        await _send(ws, {"type": "chat", "role": "system",
                         "text": "规划团队遇到错误，请查看状态时间线或重试。"}, sess)


async def _sender(ws: WebSocket, sess: Session) -> None:
    """WS 发送协程：状态总线 + 团队事件双路复用（持久任务，避免漏消费）。

    任何单个事件的处理失败只降级为时间线错误提示，绝不终止推送循环——
    此前转述（LLM 调用）异常冒泡会烧掉本协程，用户从此收不到草稿/PDF 卡片。
    断开/取消时退订本会话总线（多会话隔离：不残留订阅、不串台）。
    """
    sub = sess.bus.subscribe()
    status_task = asyncio.create_task(sub.get())
    event_task = asyncio.create_task(sess.team_events.get())
    try:
        while True:
            done, _ = await asyncio.wait({status_task, event_task},
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                is_status = t is status_task  # 必须先判定再重建，否则重赋值后恒为 False
                if is_status:
                    status_task = asyncio.create_task(sub.get())
                else:
                    event_task = asyncio.create_task(sess.team_events.get())
                try:
                    item = t.result()
                except Exception as e:  # noqa: BLE001 — 队列读取兜底
                    await _push_stream_error(sess, f"推送通道异常：{e}")
                    continue
                if is_status:
                    await _send(ws, item)  # STATUS_* / AGENT_MESSAGE
                else:
                    try:
                        await _handle_team_event(ws, sess, item)
                    except Exception as e:  # noqa: BLE001 — 单事件失败不影响后续推送
                        await _push_stream_error(sess, f"成果推送环节出错（不影响规划数据）：{e}")
    finally:
        status_task.cancel()
        event_task.cancel()
        sess.bus.unsubscribe(sub)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    sid, sess = _get_session(ws.query_params.get("sid"))
    # 断线重连补发：最近状态历史 + 当前画像 + 草稿/成品（风险 #7）
    for ev in sess.bus.history():
        await _send(ws, ev)
    await _send(ws, {"type": "session", "sid": sid, "title": _session_title(sess)})
    await _send(ws, {"type": "profile", "profile": sess.profile_snapshot()})
    if sess.bb.profile.draft:
        await _send(ws, {"type": "draft", "html": _render_draft_html(sess)})
    if sess.bb.profile.final:
        f = sess.bb.profile.final
        await _send(ws, {"type": "final", "pdf_url": f.pdf_url, "orders": f.order_summary,
                         "total_price": f.total_price})

    sender = asyncio.create_task(_sender(ws, sess))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = msg.get("type")
            if kind == "ping":
                await _send(ws, {"type": "pong", "ts": msg.get("ts")})
            elif kind == "stop":
                receipt = sess.runner.cancel(reason="用户点击停止按钮")
                text = ("已停止当前规划任务。已收集的攻略/车票/酒店数据保留，"
                        "补充信息后可重新启动。") if receipt["status"] == "cancelled" \
                    else "当前没有进行中的规划任务。"
                await _send(ws, {"type": "chat", "role": "system", "text": text}, sess)
            elif kind == "chat":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await _send(ws, {"type": "chat", "role": "user", "text": text})
                # 即时回执：走 _sender 协程异步送达时间线（刻意不用 type=chat——前端任何
                # 非 user 的 chat 都会解锁 busy，会诱导用户在处理中重复发送）
                try:
                    await sess.bus.emit("Chatter", "已收到您的消息，正在处理…", "STATUS_INFO")
                except Exception:  # noqa: BLE001 — 回执失败不影响主流程
                    pass
                try:
                    reply = await sess.handle_user_message(text)
                except Exception as e:  # noqa: BLE001 — 用户可见错误也要反馈
                    from ..status import AUDIT
                    AUDIT.output("Gateway", f"用户消息处理异常（{type(e).__name__}: {e}）")
                    reply = "（系统提示）刚才的请求没有处理成功，请稍后重试；若持续失败请重启服务。"
                await _send(ws, {"type": "chat", "role": "chatter", "text": reply or "（无回复）"}, sess)
                await _send(ws, {"type": "profile", "profile": sess.profile_snapshot()}, sess)
                await _send(ws, {"type": "usage", "usage": usage_summary()}, sess)
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=ServerConfig.HOST, port=ServerConfig.PORT, log_level="info")
