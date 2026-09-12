"""FastAPI 网关（§3.1）——多用户版（2026-09-12）。

- 认证：/api/register、/api/login、/api/me、/api/ws-ticket、/share/{id} 公开或半公开；
  其余 /api/* 与 WebSocket 一律要求登录（Bearer 令牌 / 30s WS 票据，见 auth.py）。
- 会话隔离：注册表按 "用户名/sid" 键控，/api/* 只见本人会话；持久化 sessions/{用户名}/{sid}.json；
  首个注册用户一次性继承旧单用户数据（sessions/*.json 平移，只移动不重写）。
- PDF 交付：无鉴权的 /outputs 静态挂载已移除 → GET /api/pdf?sid=（登录+归属，
  文件名一律服务端从该会话定稿记录派生，路径参数面归零）；/share/{id} 免登录直链（可撤销）。
- 偏好记忆：定稿事件触发后台提炼（memory.py，失败不影响主流程）；/api/memory 管理端点。
- 内存态：服务重启后会话丢失（磁盘快照自动恢复），与旧版一致。
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import auth
from ..config import BASE_DIR, OUTPUT_DIR, ServerConfig, SESSIONS_DIR
from ..llm import usage_summary
from .. import maintenance
from .. import memory
from ..pdf_templates import get_template, list_templates
from ..planning import compute_budget
from ..session import Session
from ..status import AUDIT, event_json

from ..persistence import safe_sid


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """启动钩子：自动清理 outputs 中确定无引用的产物（保护窗内绝不删，失败不阻塞启动）。
    停机钩子：等待/补写所有会话的在途异步落盘（防抖后的尾随增量不丢）。"""
    try:
        plan = maintenance.plan_cleanup()   # 默认保护窗：无引用 PDF 3 天 / 无引用图 24h
        if plan.total_files:
            result = maintenance.apply(plan)
            AUDIT.output("Gateway", f"启动清理：删除 {len(result.deleted)} 个无用文件，"
                                    f"释放 {result.freed_bytes / 1e6:.1f} MB")
    except maintenance.MaintenanceAborted as e:
        AUDIT.output("Gateway", f"启动清理中止（fail-closed，不影响服务）：{e}")
    except Exception as e:  # noqa: BLE001 — 清理失败绝不影响服务
        AUDIT.output("Gateway", f"启动清理失败（不影响服务）：{type(e).__name__}: {e}")
    yield
    for sess in sessions.values():
        try:
            await sess.flush()
        except Exception:  # noqa: BLE001 — 停机落盘失败不阻塞进程退出
            pass


app = FastAPI(title="TripMate 多 Agent 协同旅游规划系统", lifespan=lifespan)

# sender 协作交接的空闲检查间隔（秒）：无事件在途时超时醒来做身份检查，stale 即退出
_SENDER_IDLE_TIMEOUT_S = 60.0

_env = Environment(loader=FileSystemLoader(BASE_DIR / "tripmate" / "templates"),
                   autoescape=select_autoescape(["html"]))

# ---- 会话注册表（多用户隔离）：键 = "用户名/sid"，两个成分都经过硬校验 ----
sessions: dict[str, Session] = {}


def _skey(username: str, sid: str) -> str:
    return f"{username}/{safe_sid(sid or '')}"


def _sid_of(key: str) -> str:
    return key.split("/", 1)[1]


def _get_session(username: str, sid: str | None) -> tuple[str, Session]:
    """按用户+sid 取会话；未知合法组合视为新会话注册（前端切换/刷新天然幂等，状态自动恢复）。"""
    key = _skey(username, sid or "")
    if key not in sessions:
        sessions[key] = Session(safe_sid(sid or ""), username=username)
    return key, sessions[key]


# ---- 认证 ----

async def _auth_user(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") \
        else (request.query_params.get("token") or "")
    username = auth.verify_token(token)
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return username


async def _migrate_legacy_sessions(username: str) -> None:
    """首个注册用户继承旧单用户数据：平移 sessions/*.json 到其名下（只移动不重写，失败不阻塞）。"""
    try:
        legacy = [p for p in SESSIONS_DIR.glob("*.json")]
        if not legacy:
            return
        target = SESSIONS_DIR / username
        target.mkdir(parents=True, exist_ok=True)
        moved = 0
        for p in legacy:
            dest = target / p.name
            if not dest.exists():
                p.replace(dest)
                moved += 1
        if moved:
            AUDIT.output("Gateway", f"已向首个用户 {username} 迁移 {moved} 个旧会话文件")
    except OSError as e:
        AUDIT.output("Gateway", f"旧会话迁移失败（{type(e).__name__}: {e}），不影响注册")


@app.post("/api/register")
async def register(payload: dict = Body(...)) -> JSONResponse:
    try:
        result = await auth.register(str(payload.get("username") or ""),
                                     str(payload.get("password") or ""))
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await _migrate_legacy_sessions(result["username"])
    return JSONResponse(result)


@app.post("/api/login")
async def login(payload: dict = Body(...)) -> JSONResponse:
    try:
        result = await auth.login(str(payload.get("username") or ""),
                                  str(payload.get("password") or ""))
    except auth.AuthError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse(result)


@app.get("/api/me")
async def me(user: str = Depends(_auth_user)) -> JSONResponse:
    return JSONResponse(auth.user_info(user) or {"username": user, "is_admin": False})


@app.post("/api/ws-ticket")
async def ws_ticket(user: str = Depends(_auth_user)) -> JSONResponse:
    """换 30 秒 WS 握手票据：长期令牌不进 URL（浏览器 WebSocket 无法带 Authorization 头）。"""
    return JSONResponse({"ticket": auth.issue_ws_ticket(user)})


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


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/sessions")
async def list_sessions(user: str = Depends(_auth_user)) -> JSONResponse:
    out = [{"sid": _sid_of(key), "title": _session_title(sess)}
           for key, sess in sessions.items() if key.split("/", 1)[0] == user]
    return JSONResponse(out)


@app.post("/api/sessions")
async def create_session(user: str = Depends(_auth_user)) -> JSONResponse:
    sid = f"s-{uuid.uuid4().hex[:8]}"
    key = _skey(user, sid)
    sessions[key] = Session(safe_sid(sid), username=user)
    return JSONResponse({"sid": sid, "title": _session_title(sessions[key])})


@app.get("/api/profile")
async def profile(sid: str = "default", user: str = Depends(_auth_user)) -> JSONResponse:
    _, sess = _get_session(user, sid)
    return JSONResponse(sess.profile_snapshot())


@app.get("/api/usage")
async def usage(sid: str | None = None, user: str = Depends(_auth_user)) -> JSONResponse:
    """带 sid → 该会话当前规划 run 的消耗（per-run 基线，多用户互不污染）；
    不带 → 进程累计总量。"""
    if sid:
        _, sess = _get_session(user, sid)
        return JSONResponse(usage_summary(sess.runner._usage_baseline))
    return JSONResponse(usage_summary())


@app.get("/api/templates")
async def templates() -> JSONResponse:
    """PDF 模板列表（定稿时按黑板 basic_info.template 渲染）。"""
    return JSONResponse({"templates": list_templates()})


def _final_pdf_basename(sess: Session) -> str | None:
    """会话当前成品 PDF 文件名（从定稿记录取 basename，路径参数面归零）。"""
    final = sess.bb.profile.final
    if not final or not final.pdf_url:
        return None
    name = final.pdf_url.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return name if name.lower().endswith(".pdf") else None


def _api_pdf_url(sid: str, sess: Session) -> str | None:
    name = _final_pdf_basename(sess)
    if not name:
        return None
    return f"/api/pdf?sid={quote(safe_sid(sid or 'default'))}"


@app.get("/api/pdf")
async def serve_pdf(sid: str = "default", user: str = Depends(_auth_user)):
    _, sess = _get_session(user, sid)
    name = _final_pdf_basename(sess)
    path = OUTPUT_DIR / name if name else None
    if not name or path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="该会话还没有成品 PDF")
    return FileResponse(path, media_type="application/pdf", filename=name)


# ---- PDF 分享直链（免登录，链接即凭证；创建时快照文件名） ----

from ..maintenance import SHARES_PATH as _SHARES_PATH  # noqa: E402 — 分享索引与清理共用同一常量
_SHARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_share_lock = asyncio.Lock()


def _load_shares() -> dict:
    try:
        if not _SHARES_PATH.exists():
            return {}
        data = json.loads(_SHARES_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError) as e:
        AUDIT.output("Gateway", f"分享索引读取失败（{type(e).__name__}: {e}），按空处理")
        return {}


def _save_shares(shares: dict) -> None:
    tmp = _SHARES_PATH.with_name(_SHARES_PATH.name + f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(shares, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(_SHARES_PATH)


@app.post("/api/share")
async def create_share(payload: dict = Body(...), user: str = Depends(_auth_user)) -> JSONResponse:
    sid = str(payload.get("sid") or "default")
    _, sess = _get_session(user, sid)
    safe = safe_sid(sid)
    name = _final_pdf_basename(sess)
    if not name:
        raise HTTPException(status_code=404, detail="该会话还没有成品 PDF 可分享")
    async with _share_lock:
        shares = _load_shares()
        for share_id, info in shares.items():  # 幂等：同会话同文件复用现有未撤销链接
            if (not info.get("revoked") and info.get("user") == user
                    and info.get("sid") == safe and info.get("file") == name):
                return JSONResponse({"share_id": share_id, "url": f"/share/{share_id}"})
        share_id = secrets.token_urlsafe(12)
        shares[share_id] = {"user": user, "sid": safe, "file": name,
                            "created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        _save_shares(shares)
    return JSONResponse({"share_id": share_id, "url": f"/share/{share_id}"})


@app.delete("/api/share/{share_id}")
async def revoke_share(share_id: str, user: str = Depends(_auth_user)) -> JSONResponse:
    if not _SHARE_ID_RE.match(share_id):
        raise HTTPException(status_code=404, detail="分享不存在")
    async with _share_lock:
        shares = _load_shares()
        info = shares.get(share_id)
        if not info or info.get("user") != user:
            raise HTTPException(status_code=404, detail="分享不存在")
        info["revoked"] = True
        _save_shares(shares)
    return JSONResponse({"ok": True})


@app.get("/share/{share_id}")
async def get_share(share_id: str):
    """免登录分享直链：链接即凭证（不可猜测 id）；撤销后 404。"""
    if not _SHARE_ID_RE.match(share_id):
        raise HTTPException(status_code=404, detail="分享不存在或已撤销")
    info = _load_shares().get(share_id)
    file = (info or {}).get("file") or ""
    if (info or {}).get("revoked") or not file or "/" in file or "\\" in file or not file.lower().endswith(".pdf"):
        raise HTTPException(status_code=404, detail="分享不存在或已撤销")
    path = OUTPUT_DIR / file
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件已不存在")
    return FileResponse(path, media_type="application/pdf", filename=file)


# ---- 偏好记忆管理 ----

@app.get("/api/memory")
async def get_memory(user: str = Depends(_auth_user)) -> JSONResponse:
    return JSONResponse(memory.load_memory(user))


@app.delete("/api/memory/{pref_id}")
async def delete_memory(pref_id: str, user: str = Depends(_auth_user)) -> JSONResponse:
    return JSONResponse({"ok": memory.delete_preference(user, pref_id)})


@app.delete("/api/memory")
async def clear_memory(user: str = Depends(_auth_user)) -> JSONResponse:
    memory.clear_memory(user)
    return JSONResponse({"ok": True})


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


async def _capture_memory(sess: Session) -> None:
    """定稿后沉淀偏好记忆（后台任务，失败不影响任何主流程）；成功则置脏等下次消息热重建。"""
    try:
        ok = await memory.capture_from_profile(sess.username, sess.bb.profile)
        if ok:
            sess.memory_dirty = True
    except Exception as e:  # noqa: BLE001 — 双保险（capture 自身已兜底）
        AUDIT.output("Gateway", f"偏好沉淀任务异常（{type(e).__name__}: {e}）")


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
        await _send(_route_ws(sess, ws), {"type": "final",
                                          "pdf_url": _api_pdf_url(safe_sid(sess.sid), sess) or data.pdf_url,
                                          "orders": data.order_summary,
                                          "total_price": data.total_price}, sess)
        await _send(_route_ws(sess, ws), {"type": "usage", "usage": usage_summary(sess.runner._usage_baseline)})  # 定稿即刷新消耗条
        if sess.username:
            asyncio.create_task(_capture_memory(sess))
        reply = await sess.relay_team_event(
            "规划团队已完成定稿（黑板 final 分区：PDF + 推荐订单清单）。请读取后向用户转述成果要点，"
            "提醒逐项确认订单并自行在官方渠道支付。")
        await _chat(ws, sess, {"type": "chat", "role": "chatter", "text": reply})
    elif kind == "error":
        await _chat(ws, sess, {"type": "chat", "role": "system",
                               "text": "规划团队遇到错误，请查看状态时间线或重试。"})


def _cancel_current_chat(sess: Session) -> bool:
    """stop 按钮联动：中断正在进行的用户消息处理（此前 stop 消息压在 socket 缓冲里
    根本读不到——用户无法停止卡死的处理）。返回是否发生了中断。"""
    t = sess.current_chat_task
    if t is not None and not t.done():
        t.cancel()
        return True
    return False


async def _chat_worker(ws: WebSocket, sess: Session, q: asyncio.Queue) -> None:
    """聊天处理 worker（2026-09-05 修复 #10）：WS 接收循环只入队，处理在独立任务串行进行——
    处理期间（最坏 ~30 分钟）stop 按钮与前端 25s 心跳 ping 此前全部失灵。

    FIFO 队列保证多条消息按序处理（与原串行语义一致）；chatter_lock 继续承担
    跨连接/转述的互斥。"""
    while True:
        text = await q.get()
        task = asyncio.create_task(sess.handle_user_message(text))
        sess.current_chat_task = task
        try:
            reply = await task
        except asyncio.CancelledError:
            if task.cancelled():
                # stop 中断当前处理：与超时路径同源——重建 Chatter 丢弃被中断的上下文
                sess.rebuild_chatter()
                await _chat(ws, sess, {"type": "chat", "role": "system",
                                       "text": "已停止当前消息处理。"})
                continue
            task.cancel()  # worker 自身被取消（连接断开）：子任务一并终止后透传
            sess.rebuild_chatter()
            raise
        except Exception as e:  # noqa: BLE001 — 用户可见错误也要反馈
            AUDIT.output("Gateway", f"用户消息处理异常（{type(e).__name__}: {e}）")
            reply = "（系统提示）刚才的请求没有处理成功，请稍后重试；若持续失败请重启服务。"
        finally:
            sess.current_chat_task = None
        await _chat(ws, sess, {"type": "chat", "role": "chatter", "text": reply or "（无回复）"})
        await _send(_route_ws(sess, ws), {"type": "profile", "profile": sess.profile_snapshot()}, sess)
        await _send(_route_ws(sess, ws), {"type": "usage", "usage": usage_summary(sess.runner._usage_baseline)}, sess)


async def _sender(ws: WebSocket, sess: Session, sub=None, last_seq: int = 0) -> None:
    """WS 发送协程：状态总线 + 团队事件双路复用（持久任务，避免漏消费）。

    sub/last_seq（修复 #17 补播窗口）：ws_endpoint 先订阅再补播——补播期间产生的事件
    落在订阅队列里不丢；sender 按 seq > last_seq 丢弃与补播历史重复的条目。缺省参数
    保持旧调用（订阅在协程内创建、不去重）兼容。

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
    if sub is None:
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
                    if not stale and item.get("seq", 0) > last_seq:  # 补播窗口去重（修复 #17）
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
            await _send(ws, {"type": "final", "pdf_url": _api_pdf_url(sid, sess) or f.pdf_url,
                             "orders": f.order_summary, "total_price": f.total_price})
        except Exception as e:  # noqa: BLE001 — 成品卡补播失败只降级
            AUDIT.output("Gateway", f"成品卡补播失败已跳过（{type(e).__name__}: {e}）")
    return True


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    username = auth.verify_ws_ticket(ws.query_params.get("ticket") or "")
    await ws.accept()
    if not username:
        try:
            await ws.close(code=4401)  # 未认证：前端停自动重连并弹登录
        except Exception:  # noqa: BLE001 — 对端可能已断开
            pass
        return
    sid, sess = _get_session(username, ws.query_params.get("sid"))
    sess._live_ws = ws  # 会话当前活动连接：迟到的回复/卡片经 _route_ws 送达本连接
    sender = None
    worker = None
    try:
        # 先订阅再补播（修复 #17 补播窗口）：补播期间产生的状态事件进入订阅队列，
        # 由 sender 按 seq > last_seq 去重后送达——不再存在"补播与订阅之间丢事件"。
        # last_seq 必须在补播快照之后取：它同时覆盖"订阅→快照之间"（历史+队列双份）的窗口
        sub = sess.bus.subscribe()
        history_n, chat_n = len(sess.bus.history()), len(sess.chat_history())
        if not await _replay_snapshot(ws, sid, sess):
            sess.bus.unsubscribe(sub)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001 — socket 可能已断开（正是坏会话重连场景），关闭失败无需处理
                pass
            return
        last_seq = sess.bus.last_seq()
        AUDIT.output("Gateway", f"WS 连接 user={username} sid={sid}｜补播状态 {history_n} 条 / 聊天 {chat_n} 条")

        # 协作式交接（2026-09-05）：不强杀旧 sender——旧 sender 处理完手头事件后
        # 自检"已非当前 sender"退出；在途团队事件经 _route_ws 送达本连接，不丢失。
        sender = asyncio.create_task(_sender(ws, sess, sub=sub, last_seq=last_seq))
        chat_q: asyncio.Queue = asyncio.Queue()
        worker = asyncio.create_task(_chat_worker(ws, sess, chat_q))
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
                        interrupted = _cancel_current_chat(sess)
                        text = ("已停止当前规划任务。已收集的攻略/车票/酒店数据保留，"
                                "补充信息后可重新启动。") if receipt["status"] == "cancelled" \
                            else "当前没有进行中的规划任务。"
                        if interrupted:
                            text += "正在处理的下一条消息也已中断。"
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
                        # 处理移入 worker（修复 #10）：接收循环立即回到 receive_text，
                        # stop/ping 在处理期间保持实时可达
                        chat_q.put_nowait(text)
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
        if worker is not None:
            worker.cancel()
        if getattr(sess, "_live_ws", None) is ws:
            sess._live_ws = None
        AUDIT.output("Gateway", f"WS 断开 user={username} sid={sid}")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=ServerConfig.HOST, port=ServerConfig.PORT, log_level="info")
