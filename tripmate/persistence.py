"""会话持久化（2026-09-05）：聊天历史 + 画像快照落盘，服务重启后新会话自动恢复。

- 存储 sessions/{sid}.json（原子写：临时文件 + os.replace，崩溃不损坏）
- sid 白名单 [A-Za-z0-9_-]{1,64}：sid 来自客户端查询参数，直接拼路径存在穿越风险，
  非法 sid 一律回落 default
- 只持久化聊天历史与画像快照（不含 changelog：重启后检查点基线重置，旧条目无用，
  且防止文件随会话增长无限膨胀）；版本号保留。运行中的团队任务与状态时间线不恢复。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid

from .config import SESSIONS_DIR
from .status import AUDIT

_SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def safe_sid(sid: str) -> str:
    """sid 白名单校验（三分支）：空 → default；合法 → 原样；非法非空 → junk-{md5[:8]}。

    sid 来自客户端查询参数，直接拼文件路径存在穿越风险。非法 sid 不回落 default——
    default 会话已持久化用户聊天记录，合入会造成跨来源隔离失效；改用确定性脱敏键，
    同一非法 sid 的浏览器刷新后仍能取回自己的历史（隔离且稳定）。"""
    sid = (sid or "").strip()
    if not sid:
        return "default"
    if _SID_RE.match(sid):
        return sid
    return "junk-" + hashlib.md5(sid.encode()).hexdigest()[:8]


def session_path(sid: str):
    return SESSIONS_DIR / f"{safe_sid(sid)}.json"


def load_session(sid: str) -> dict | None:
    """读取持久化状态；不存在/损坏返回 None（按全新会话处理，绝不抛异常）。"""
    path = session_path(sid)
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError) as e:
        AUDIT.output("Persistence", f"会话状态读取失败（{type(e).__name__}: {e}），按全新会话处理")
        return None


def save_session(sid: str, chat_history: list[dict], profile: dict) -> None:
    """原子写入会话状态；失败只记日志，绝不影响聊天/规划主流程。"""
    path = session_path(sid)
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(json.dumps({
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "chat_history": chat_history,
            "profile": profile,
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        AUDIT.output("Persistence", f"会话状态保存失败（{type(e).__name__}: {e}）")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
