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
from ..pdf_templates import get_template, list_templates
from ..planning import compute_budget
from ..session import Session
from ..status import AUDIT, event_json

from ..persistence import safe_sid

app = FastAPI(title="TripMate 多 Agent 协同旅游规划系统")

# sender 协作交接的空闲检查间隔（秒）：无事件在途时超时醒来做身份检查，stale 即退出
_SENDER_IDLE_TIMEOUT_S = 60.0

_env = Environment(loader=FileSystemLoader(BASE_DIR / "tripmate" / "templates"),
                   autoescape=select_autoescape(["html"]))

# ---- 会话注册表（需求 2）----
DEFAULT_SID = "default"
# default 会话带 sid 构造：持久化启用（无 ?sid 的单用户场景也要落盘/重启恢复，2026-09-05）
sessions: dict[str, Session] = {DEFAULT_SID: Session(DEFAULT_SID)}


def _get_session(sid: str | None) -> tuple[str, Session]:
    """按 sid 取会话；sid 经白名单校验（非法 → junk-{md5} 脱敏键，防路径穿越且不污染 default）；
    未知合法 sid 视为新会话注册（前端切换/刷新天然幂等，持久化状态自动恢复）。"""
    key = safe_sid(sid or "")
    if key not in sessions:
        sessions[key] = Session(key)
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


@app.get("/api/templates")
async def templates() -> JSONResponse:
    """PDF 模板列表（前端下拉选择；定稿时按黑板 basic_info.template 渲染）。"""
    return JSONResponse({"templates": list_templates()})


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


def _route_ws(sess: Session | None, ws: WebSocket) -> WebSocket:
    """路由到会话当前活动连接：处理期间刷新/断连后完成的回复，也能送达新连接
    （2026-09-05 根治"迟到回复发往死旧 socket、新连接永远看不到"）；无活动连接时
    回退调用方自带 ws（兼容测试桩）。"""
    return getattr(sess, "_live_ws", None) or ws


async def _send(ws: WebSocket, payload: dict, sess: Session | None = None) -> None:
    try:
        await ws.send_text(event_json(payload))
    except Exception:  # noqa: BLE001 — 客户端断开由外层统一处理；
        # chat 消息在创建点已入 sess.chat_history（见 _chat），断线重连后全量补播，不再丢
        pass


async def _chat(ws: WebSocket, sess: Session | None, payload: dict) -> None:
    """聊天消息统一出口：创建点先记录（恰好一次，与发送成败无关）再发送。
    发送目标为会话当前活动连接（旧处理协程产出的回复也能送达刷新后的新连接）。

    断线时发送失败不丢——重连补播 sess.chat_history() 全量重建聊天面板
    （2026-09-03 根治"浏览器刷新/关闭导致回复传不到用户"；2026-09-05 路由到活动连接）。"""
    if sess is not None:
        try:
            sess.record_chat(payload)
        except Exception:  # noqa: BLE001 — 历史记录失败不影响发送
            pass
    await _send(_route_ws(sess, ws), payload, sess)


async def _push_stream_error(sess: Session, text: str) -> None:
    """向时间线推送一条错误状态（best-effort：失败即丢弃，绝不向上抛）。"""
    try:
        await sess.bus.emit("TeamRunner", text, "STATUS_ERROR")
    except Exception:  # noqa: BLE001
        pass


async def _handle_team_event(ws: WebSocket, sess: Session, item) -> None:
    """团队事件 → 前端卡片 + Chatter 转述。卡片先于转述推送（转述挂了卡片也已送达）。
    卡片经 _route_ws 送会话当前活动连接（旧连接的移交事件也能送达刷新后的新页面）。"""
    kind, data = item
    if kind == "draft_ready":
        await _send(_route_ws(sess, ws), {"type": "draft", "html": _render_draft_html(sess),
                                          "draft": sess.bb.profile.draft.model_dump(mode="json"),
                                          "budget": compute_budget(sess.bb.profile,
                                                                   sess.bb.profile.draft)}, sess)
        reply = await sess.relay_team_event(
            "规划团队已产出行程草稿（黑板 draft 分区已就绪）。请读取后向用户转述逐日概要与预算结论，"
            "并询问是否需要修改（用户可提出修改意见或确认）。")
        await _chat(ws, sess, {"type": "chat", "role": "chatter", "text": reply})
    elif kind == "completed":
        await _send(_route_ws(sess, ws), {"type": "final", "pdf_url": data.pdf_url,
                                          "orders": data.order_summary,
                                          "total_price": data.total_price}, sess)
        await _send(_route_ws(sess, ws), {"type": "usage", "usage": usage_summary()})  # 定稿即刷新消耗条（不等下次聊天）
        reply = await sess.relay_team_event(
            "规划团队已完成定稿（黑板 final 分区：PDF + 推荐订单清单）。请读取后向用户转述成果要点，"
            "提醒逐项确认订单并自行在官方渠道支付。")
        await _chat(ws, sess, {"type": "chat", "role": "chatter", "text": reply})
    elif kind == "error":
        await _chat(ws, sess, {"type": "chat", "role": "system",
                               "text": "规划团队遇到错误，请查看状态时间线或重试。"})


async def _sender(ws: WebSocket, sess: Session) -> None:
    """WS 发送协程：状态总线 + 团队事件双路复用（持久任务，避免漏消费）。

    协作式交接（2026-09-05）：启动时自认领 sess._sender_task；新连接的 sender 认领后，
    旧 sender 成为 stale——状态事件丢弃副本（新 sender 订阅队列有自己的同条副本，
    不丢不重），团队事件仍必须处理（单消费者队列，丢弃即真丢）并经 _route_ws 送当前
    活动连接；处理完手头事件即退出。asyncio.wait 60s 超时兜底：无事件在途时 stale
    即退出，僵尸订阅寿命封顶。

    任何单个事件的处理失败只降级为时间线错误提示，绝不终止推送循环——
    此前转述（LLM 调用）异常冒泡会烧掉本协程，用户从此收不到草稿/PDF 卡片。
    断开/取消时退订本会话总线（多会话隔离：不残留订阅、不串台）。
    """
    sess._sender_task = asyncio.current_task()
    sub = sess.bus.subscribe()
    status_task = asyncio.create_task(sub.get())
    event_task = asyncio.create_task(sess.team_events.get())
    try:
        while True:
            done, _ = await asyncio.wait({status_task, event_task}, timeout=_SENDER_IDLE_TIMEOUT_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            stale = asyncio.current_task() is not sess._sender_task
            if not done:
                if stale:
                    break  # 60s 无事件且已被新连接接管：退出（防僵尸订阅累积）
                continue
            for t in done:
                is_status = t is status_task  # 必须先判定再重建，否则重赋值后恒为 False
                if is_status:
                    status_task = asyncio.create_task(sub.get())
                else:
                    event_task = asyncio.create_task(sess.team_events.get())
                try:
                    item = t.result()
                except Exception as e:  # noqa: BLE001 — 队列读取兜底
                    if not stale:
                        await _push_stream_error(sess, f"推送通道异常：{e}")
                    continue
                if is_status:
                    if not stale:  # stale 副本丢弃：新 sender 有同条副本，避免重复推送
                        await _send(ws, item)  # STATUS_* / AGENT_MESSAGE
                else:
                    try:
                        await _handle_team_event(ws, sess, item)  # 团队事件单消费者，stale 也必须处理
                    except Exception as e:  # noqa: BLE001 — 单事件失败不影响后续推送
                        if not stale:
                            await _push_stream_error(sess, f"成果推送环节出错（不影响规划数据）：{e}")
            if stale:
                break
    finally:
        status_task.cancel()
        event_task.cancel()
        sess.bus.unsubscribe(sub)


async def _replay_snapshot(ws: WebSocket, sid: str, sess: Session) -> bool:
    """断线重连补发：最近状态历史 + 聊天历史 + 当前画像 + 草稿/成品（风险 #7；聊天补播 2026-09-03）。

    补播消息带 replay 标记（浅拷贝发送，不污染存储历史）：前端据此放行补播的 user 消息渲染。
    分级隔离（2026-09-05）：历史/会话/画像补播失败 → 连接不可信，返回 False 由调用方关闭；
    草稿卡/成品卡渲染失败 → 跳过该卡片并记日志、连接保留——持久化后坏数据跨重启存活，
    单张坏卡片降级为"缺卡片"而非"整个会话打不开"（此前坏草稿会触发 2s 重连风暴锁死会话）。"""
    try:
        for ev in sess.bus.history():
            await _send(ws, {**ev, "replay": True})
        for ev in sess.chat_history():
            await _send(ws, {**ev, "replay": True})
        await _send(ws, {"type": "session", "sid": sid, "title": _session_title(sess)})
        await _send(ws, {"type": "profile", "profile": sess.profile_snapshot()})
    except Exception as e:  # noqa: BLE001 — 核心历史补播失败：连接不可信
        AUDIT.output("Gateway", f"重连补播历史失败（{type(e).__name__}: {e}），关闭本次连接")
        return False
    if sess.bb.profile.draft:
        try:
            await _send(ws, {"type": "draft", "html": _render_draft_html(sess)})
        except Exception as e:  # noqa: BLE001 — 坏草稿只降级为缺卡片
            AUDIT.output("Gateway", f"草稿卡补播失败已跳过（{type(e).__name__}: {e}）")
    if sess.bb.profile.final:
        try:
            f = sess.bb.profile.final
            await _send(ws, {"type": "final", "pdf_url": f.pdf_url, "orders": f.order_summary,
                             "total_price": f.total_price})
        except Exception as e:  # noqa: BLE001 — 成品卡补播失败只降级
            AUDIT.output("Gateway", f"成品卡补播失败已跳过（{type(e).__name__}: {e}）")
    return True


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    sid, sess = _get_session(ws.query_params.get("sid"))
    sess._live_ws = ws  # 会话当前活动连接：迟到的回复/卡片经 _route_ws 送达本连接
    sender = None
    try:
        history_n, chat_n = len(sess.bus.history()), len(sess.chat_history())
        if not await _replay_snapshot(ws, sid, sess):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — socket 可能已断开（正是坏会话重连场景），关闭失败无需处理
                pass
            return
        AUDIT.output("Gateway", f"WS 连接 sid={sid}｜补播状态 {history_n} 条 / 聊天 {chat_n} 条")

        # 协作式交接（2026-09-05）：不强杀旧 sender——旧 sender 处理完手头事件后
        # 自检"已非当前 sender"退出；在途团队事件经 _route_ws 送达本连接，不丢失。
        sender = asyncio.create_task(_sender(ws, sess))
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                try:
                    kind = msg.get("type")
                    if kind == "ping":
                        await _send(ws, {"type": "pong", "ts": msg.get("ts")})
                    elif kind == "stop":
                        receipt = sess.runner.cancel(reason="用户点击停止按钮")
                        text = ("已停止当前规划任务。已收集的攻略/车票/酒店数据保留，"
                                "补充信息后可重新启动。") if receipt["status"] == "cancelled" \
                            else "当前没有进行中的规划任务。"
                        await _chat(ws, sess, {"type": "chat", "role": "system", "text": text})
                    elif kind == "template":
                        name = (msg.get("name") or "").strip()
                        try:
                            tpl = get_template(name or None)
                        except ValueError:
                            await _chat(ws, sess, {"type": "chat", "role": "system",
                                                   "text": f"未知的模板样式 '{name}'，请重新选择。"})
                            continue
                        try:
                            await sess.bb.apply_basic_info(
                                {"template": tpl.name}, "chatter", "用户选择 PDF 模板")
                        except Exception as e:  # noqa: BLE001 — 模板写库失败不烧连接（2026-09-04 加固）
                            AUDIT.output("Gateway", f"模板切换写入失败（{type(e).__name__}: {e}）")
                            await _chat(ws, sess, {"type": "chat", "role": "system",
                                                   "text": "模板切换没有成功，请稍后再试一次。"})
                            continue
                        await _chat(ws, sess, {"type": "chat", "role": "system",
                                               "text": f"已切换路书样式为「{tpl.display_name}」，定稿时将使用该模板。"})
                        await _send(_route_ws(sess, ws), {"type": "profile", "profile": sess.profile_snapshot()}, sess)
                    elif kind == "chat":
                        text = (msg.get("text") or "").strip()
                        if not text:
                            continue
                        await _chat(ws, sess, {"type": "chat", "role": "user", "text": text})
                        # 即时回执：走 _sender 协程异步送达时间线（刻意不用 type=chat——前端任何
                        # 非 user 的 chat 都会解锁 busy，会诱导用户在处理中重复发送）
                        try:
                            await sess.bus.emit("Chatter", "已收到您的消息，正在处理…", "STATUS_INFO")
                        except Exception:  # noqa: BLE001 — 回执失败不影响主流程
                            pass
                        try:
                            reply = await sess.handle_user_message(text)
                        except Exception as e:  # noqa: BLE001 — 用户可见错误也要反馈
                            AUDIT.output("Gateway", f"用户消息处理异常（{type(e).__name__}: {e}）")
                            reply = "（系统提示）刚才的请求没有处理成功，请稍后重试；若持续失败请重启服务。"
                        await _chat(ws, sess, {"type": "chat", "role": "chatter", "text": reply or "（无回复）"})
                        await _send(_route_ws(sess, ws), {"type": "profile", "profile": sess.profile_snapshot()}, sess)
                        await _send(_route_ws(sess, ws), {"type": "usage", "usage": usage_summary()}, sess)
                except Exception as e:  # noqa: BLE001 — 单条消息处理失败不烧连接（2026-09-04 加固：
                    # 此前仅捕获 WebSocketDisconnect，任意分支异常都会炸掉 WS 处理器）
                    AUDIT.output("Gateway", f"WS 消息处理异常（{type(e).__name__}: {e}）")
        except WebSocketDisconnect:
            pass
    except WebSocketDisconnect:
        pass
    finally:
        if sender is not None:
            sender.cancel()
        if getattr(sess, "_live_ws", None) is ws:
            sess._live_ws = None
        AUDIT.output("Gateway", f"WS 断开 sid={sid}")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=ServerConfig.HOST, port=ServerConfig.PORT, log_level="info")
