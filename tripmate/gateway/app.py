"""FastAPI 网关（§3.1）：WebSocket 双向通信（聊天输入 + STATUS_* 状态推送）+ 静态资源。"""

from __future__ import annotations

import asyncio
import json

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

session = Session()

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/profile")
async def profile() -> JSONResponse:
    return JSONResponse(session.profile_snapshot())


@app.get("/api/usage")
async def usage() -> JSONResponse:
    return JSONResponse(usage_summary())


def _render_draft_html() -> str:
    draft = session.bb.profile.draft
    if not draft:
        return ""
    budget = compute_budget(session.bb.profile, draft)
    tpl = _env.get_template("draft.html")
    return tpl.render(draft=draft, budget=budget,
                      basic=session.bb.profile.basic_info,
                      detail=session.bb.profile.detail_info)


def _orders_payload() -> list[dict]:
    final = session.bb.profile.final
    return (final.order_summary if final else []) or [
        *[{"type": "车票", "name": t.train_no, "amount": t.price,
           "link": t.link, "reason": t.reason, "selected": t.selected,
           "reference_only": t.reference_only} for t in session.bb.profile.tickets],
        *[{"type": "酒店", "name": h.name, "amount": h.price_per_night,
           "link": h.link, "reason": h.reason, "selected": h.selected,
           "reference_only": h.reference_only} for h in session.bb.profile.hotels],
    ]


async def _send(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_text(event_json(payload))
    except Exception as e:  # noqa: BLE001 — 客户端断开由外层统一处理
        from ..status import AUDIT
        AUDIT.output("TeamRunner", f"WS 发送失败（{type(e).__name__}: {e}）：payload type={payload.get('type')}")


async def _push_stream_error(text: str) -> None:
    """向时间线推送一条错误状态（best-effort：失败即丢弃，绝不向上抛）。"""
    try:
        await session.bus.emit("TeamRunner", text, "STATUS_ERROR")
    except Exception:  # noqa: BLE001
        pass


async def _handle_team_event(ws: WebSocket, item) -> None:
    """团队事件 → 前端卡片 + Chatter 转述。卡片先于转述推送（转述挂了卡片也已送达）。"""
    kind, data = item
    if kind == "draft_ready":
        await _send(ws, {"type": "draft", "html": _render_draft_html(),
                         "draft": session.bb.profile.draft.model_dump(mode="json"),
                         "budget": compute_budget(session.bb.profile,
                                                  session.bb.profile.draft)})
        reply = await session.relay_team_event(
            "规划团队已产出行程草稿（黑板 draft 分区已就绪）。请读取后向用户转述逐日概要与预算结论，"
            "并询问是否需要修改（用户可提出修改意见或确认）。")
        await _send(ws, {"type": "chat", "role": "chatter", "text": reply})
    elif kind == "completed":
        await _send(ws, {"type": "final", "pdf_url": data.pdf_url,
                         "orders": data.order_summary,
                         "total_price": data.total_price})
        reply = await session.relay_team_event(
            "规划团队已完成定稿（黑板 final 分区：PDF + 推荐订单清单）。请读取后向用户转述成果要点，"
            "提醒逐项确认订单并自行在官方渠道支付。")
        await _send(ws, {"type": "chat", "role": "chatter", "text": reply})
    elif kind == "error":
        await _send(ws, {"type": "chat", "role": "system",
                         "text": "规划团队遇到错误，请查看状态时间线或重试。"})


async def _sender(ws: WebSocket) -> None:
    """WS 发送协程：状态总线 + 团队事件双路复用（持久任务，避免漏消费）。

    任何单个事件的处理失败只降级为时间线错误提示，绝不终止推送循环——
    此前转述（LLM 调用）异常冒泡会烧掉本协程，用户从此收不到草稿/PDF 卡片。
    """
    sub = session.bus.subscribe()
    status_task = asyncio.create_task(sub.get())
    event_task = asyncio.create_task(session.team_events.get())
    try:
        while True:
            done, _ = await asyncio.wait({status_task, event_task},
                                         return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                is_status = t is status_task  # 必须先判定再重建，否则重赋值后恒为 False
                if is_status:
                    status_task = asyncio.create_task(sub.get())
                else:
                    event_task = asyncio.create_task(session.team_events.get())
                try:
                    item = t.result()
                except Exception as e:  # noqa: BLE001 — 队列读取兜底
                    await _push_stream_error(f"推送通道异常：{e}")
                    continue
                if is_status:
                    await _send(ws, item)  # STATUS_* / AGENT_MESSAGE
                else:
                    try:
                        await _handle_team_event(ws, item)
                    except Exception as e:  # noqa: BLE001 — 单事件失败不影响后续推送
                        await _push_stream_error(f"成果推送环节出错（不影响规划数据）：{e}")
    finally:
        status_task.cancel()
        event_task.cancel()
        session.bus.unsubscribe(sub)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    # 断线重连补发：最近状态历史 + 当前画像 + 草稿/成品（风险 #7）
    for ev in session.bus.history():
        await _send(ws, ev)
    await _send(ws, {"type": "profile", "profile": session.profile_snapshot()})
    if session.bb.profile.draft:
        await _send(ws, {"type": "draft", "html": _render_draft_html()})
    if session.bb.profile.final:
        f = session.bb.profile.final
        await _send(ws, {"type": "final", "pdf_url": f.pdf_url, "orders": f.order_summary,
                         "total_price": f.total_price})

    sender = asyncio.create_task(_sender(ws))
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
                receipt = session.runner.cancel(reason="用户点击停止按钮")
                text = ("已停止当前规划任务。已收集的攻略/车票/酒店数据保留，"
                        "补充信息后可重新启动。") if receipt["status"] == "cancelled" \
                    else "当前没有进行中的规划任务。"
                await _send(ws, {"type": "chat", "role": "system", "text": text})
            elif kind == "chat":
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                await _send(ws, {"type": "chat", "role": "user", "text": text})
                try:
                    reply = await session.handle_user_message(text)
                except Exception as e:  # noqa: BLE001 — 用户可见错误也要反馈
                    reply = f"（系统异常：{e}）"
                await _send(ws, {"type": "chat", "role": "chatter", "text": reply or "（无回复）"})
                await _send(ws, {"type": "profile", "profile": session.profile_snapshot()})
                await _send(ws, {"type": "usage", "usage": usage_summary()})
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=ServerConfig.HOST, port=ServerConfig.PORT, log_level="info")


if __name__ == "__main__":
    main()
