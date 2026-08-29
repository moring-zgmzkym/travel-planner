"""状态推送总线（§2.3 延迟 ≤2s）+ Agent 审计日志（验收 #16：Thought→Action→Observation 链路）。

STATUS_* 事件面向用户（经网关 WebSocket 推送）；Thought 属 Agent 私有推理，只进审计日志、
不进对外状态推送（§3.5 隔离约束、§3.9）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from datetime import datetime

from .config import LOG_DIR


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("tripmate")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
        fh = logging.FileHandler(LOG_DIR / "tripmate.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


LOGGER = _setup_logger()


class StatusBus:
    """各 Agent 工作进度 → 网关 WebSocket。订阅者队列 + 环形缓冲（断线补发，风险 #7）。"""

    def __init__(self, replay_limit: int = 80) -> None:
        self._subs: list[asyncio.Queue] = []
        self._history: deque[dict] = deque(maxlen=replay_limit)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def history(self) -> list[dict]:
        return list(self._history)

    async def emit(self, agent: str, text: str, kind: str = "STATUS_PROGRESS", **extra) -> None:
        event = {
            "type": "status",
            "kind": kind,
            "agent": agent,
            "text": text,
            "ts": datetime.now().strftime("%H:%M:%S"),
            **extra,
        }
        self._history.append(event)
        LOGGER.info("[%s] %s %s", agent, kind, text)
        for q in self._subs:
            await q.put(event)

    def emit_sync(self, agent: str, text: str, kind: str = "STATUS_PROGRESS", **extra) -> None:
        """同步发射（工具在非 async 上下文兜底用，事件仍异步投递）。"""
        asyncio.get_running_loop().create_task(self.emit(agent, text, kind, **extra))


class AuditLog:
    """ReAct 审计：Thought/Action/Observation/产出 只写日志文件，作为验收 #16 证据。"""

    def thought(self, agent: str, text: str) -> None:
        LOGGER.info("[ReAct][%s] Thought: %s", agent, _clip(text))

    def action(self, agent: str, tool: str, args: str) -> None:
        LOGGER.info("[ReAct][%s] Action: %s(%s)", agent, tool, _clip(args))

    def observation(self, agent: str, result: str) -> None:
        LOGGER.info("[ReAct][%s] Observation: %s", agent, _clip(result))

    def output(self, agent: str, text: str) -> None:
        LOGGER.info("[ReAct][%s] 产出: %s", agent, _clip(text))


def _clip(s: str, limit: int = 600) -> str:
    s = (s or "").replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[: limit - 3] + "..."


AUDIT = AuditLog()


def event_json(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False)
