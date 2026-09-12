"""跨会话偏好记忆（2026-09-12 多用户版）。

- 存储 data/memory/{用户名}.json：preferences（≤12 条偏好句，带 id/更新时间）+ trips（近 20 条行程摘要）
- 沉淀：规划定稿后由网关触发后台任务，LLM 从画像提炼长期偏好并与旧偏好语义合并去重；
  解析失败/提炼为空 → 保留旧偏好不写盘；任何异常只记日志，绝不影响定稿主流程
- 注入：prefs_prompt_text 生成提示词段落（Chatter system prompt / Planner 系统提示），
  明确"用户本次明示的要求永远优先"；无偏好返回空串（提示词零变化）
- 每用户文件独立；用户名先经 auth.validate_username 校验（防路径注入）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time

from autogen_core.models import UserMessage

from .auth import validate_username
from .config import DATA_DIR
from .llm import get_model_client
from .models import TravelProfile
from .status import AUDIT

logger = logging.getLogger("tripmate.memory")

MAX_PREFS = 12
_CAPTURE_TIMEOUT_S = 90.0


def memory_path(username: str):
    safe = validate_username(username)
    return DATA_DIR / "memory" / f"{safe}.json"


def load_memory(username: str) -> dict:
    path = memory_path(username)
    try:
        if not path.exists():
            return {"preferences": [], "trips": []}
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"preferences": [], "trips": []}
        data.setdefault("preferences", [])
        data.setdefault("trips", [])
        return data
    except (OSError, ValueError) as e:
        logger.warning("偏好记忆读取失败（%s: %s），按空记忆处理", type(e).__name__, e)
        return {"preferences": [], "trips": []}


def save_memory(username: str, data: dict) -> None:
    path = memory_path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def build_memory_items(texts: list[str]) -> list[dict]:
    """偏好句列表 → 带 id/时间戳的存储条目（纯函数，可单测）。"""
    items = []
    for t in texts:
        text = str(t).strip()
        if text:
            items.append({"id": secrets.token_hex(4), "text": text[:120],
                          "updated_at": time.strftime("%Y-%m-%d")})
    return items[:MAX_PREFS]


def prefs_prompt_text(username: str) -> str:
    """注入提示词的偏好段落；无偏好返回空串（system prompt 与单用户版逐字节一致）。"""
    if not username:
        return ""
    prefs = load_memory(username).get("preferences") or []
    texts = [str(p.get("text", "")).strip() for p in prefs if p.get("text")]
    if not texts:
        return ""
    lines = "\n".join(f"- {t}" for t in texts)
    return ("\n\n【用户历史偏好（从既往规划沉淀，供参考；用户本次明示的要求永远优先，"
            "与本轮输入冲突时以本轮为准）】\n" + lines)


# ---- 定稿后的自动沉淀 ----

_CAPTURE_PROMPT = """你是用户偏好档案管理员。根据用户的历史偏好档案与本次旅行规划画像，
提炼/更新该用户的长期旅行偏好（供其未来的规划参考）。要求：
- 只保留"跨旅行稳定成立的偏好"（如风格、预算习惯、酒店位置/价位偏好、饮食禁忌、节奏），
  丢弃只对本次行程成立的一次性信息（如具体日期、具体景点名、本次目的地）；
- 与历史档案语义重复的条目合并为一条，输出不超过 {max_prefs} 条；
- 每条一句话、具体可执行（"酒店偏好：春熙路商圈 300-500 元/晚"），不要空话。

历史偏好档案（可为空）：
{old_prefs}

本次规划画像（JSON）：
{profile}

只输出 JSON（不要任何其他文字）：
{{"preferences": ["偏好1", "偏好2", ...]}}
若画像信息过少、提炼不出任何稳定偏好，输出 {{"preferences": []}}。"""


def _extract_json(text: str) -> dict | None:
    """从容错文本中提取第一个完整 JSON 对象（LLM 输出可能带说明/代码围栏）。"""
    m = re.search(r"\{", text or "")
    if not m:
        return None
    depth = 0
    for i in range(m.start(), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(text[m.start():i + 1])
                    return data if isinstance(data, dict) else None
                except ValueError:
                    return None
    return None


async def capture_from_profile(username: str, profile: TravelProfile) -> bool:
    """定稿后提炼偏好并写盘。返回是否发生了写盘；任何失败只记日志返回 False。"""
    try:
        name = validate_username(username)
        old = load_memory(name)
        old_texts = [str(p.get("text", "")) for p in old.get("preferences") or []]
        digest = profile.basic_info.model_dump(mode="json")
        digest.update({"hotel": profile.detail_info.hotel.model_dump(mode="json"),
                       "food_restrictions": profile.detail_info.food_restrictions,
                       "pace": profile.detail_info.pace,
                       "style": profile.basic_info.style,
                       "budget": profile.basic_info.budget,
                       "budget_max": profile.basic_info.budget_max})
        prompt = _CAPTURE_PROMPT.format(max_prefs=MAX_PREFS,
                                        old_prefs=json.dumps(old_texts, ensure_ascii=False),
                                        profile=json.dumps(digest, ensure_ascii=False))
        client = get_model_client()
        result = await asyncio.wait_for(
            client.create([UserMessage(content=prompt, source="memory_capture")]),
            timeout=_CAPTURE_TIMEOUT_S)
        text = result.content if isinstance(result.content, str) else ""
        data = _extract_json(text)
        prefs = data.get("preferences") if data else None
        if not isinstance(prefs, list) or not prefs:
            AUDIT.output("Memory", "偏好提炼为空/解析失败，保留原档案不写盘")
            return False
        items = build_memory_items([str(p) for p in prefs if str(p).strip()])
        if not items:
            return False
        old["preferences"] = items
        final = profile.final
        if final is not None and (profile.basic_info.destination or "").strip():
            old["trips"] = ([{
                "destination": profile.basic_info.destination,
                "dates": " ~ ".join(profile.basic_info.travel_dates[:4]),
                "total_price": final.total_price,
                "finalized_at": time.strftime("%Y-%m-%d %H:%M"),
            }] + (old.get("trips") or []))[:20]
        save_memory(name, old)
        AUDIT.output("Memory", f"偏好记忆已沉淀（{name}，{len(items)} 条）")
        return True
    except Exception as e:  # noqa: BLE001 — 沉淀失败绝不影响定稿主流程
        AUDIT.output("Memory", f"偏好沉淀失败（{type(e).__name__}: {e}），跳过")
        return False


def delete_preference(username: str, pref_id: str) -> bool:
    data = load_memory(username)
    before = len(data.get("preferences") or [])
    data["preferences"] = [p for p in data.get("preferences") or [] if p.get("id") != pref_id]
    if len(data["preferences"]) == before:
        return False
    save_memory(username, data)
    return True


def clear_memory(username: str) -> None:
    save_memory(username, {"preferences": [], "trips": []})
