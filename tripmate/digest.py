"""攻略笔记提炼（PDF「景点简介/游玩活动」与「美食模块」的数据源）。

流程：① LLM 一次调用从攻略原文提炼景点/美食结构化笔记（限时 45s、容忍式 JSON
解析、字段裁剪）；② 美食逐项 Tavily 搜图（food_ 前缀落盘，单图失败跳过）。
全链路 try/except：任何失败返回空结构，PDF 侧优雅回退，绝不阻塞规划与出 PDF。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
from urllib.parse import urlsplit

import httpx
from PIL import Image as PILImage
from autogen_core.models import UserMessage

from .config import IMAGE_DIR, SearchConfig
from .llm import get_model_client
from .models import FoodNote, SpotNote
from .tools.search import _IMG_HEADERS, _WATERMARK_HOSTS

_LLM_TIMEOUT_S = 45.0    # 提炼单次上限：collect 收尾同步等待，超时即放弃走回退
_IMG_TIMEOUT_S = 12.0
_MAX_CORPUS_CHARS = 6000

_PROMPT = """你是旅行攻略编辑。下面是关于「{city}」的攻略搜索摘要原文（可能不完整）。
请从中提炼：
1. spots：最值得去的景点，最多 8 个，每个含 name（景点名）、intro（一句话简介，不超过60字）、activities（游玩活动建议，不超过60字，如"看日出/骑行/乘船/提前预约"）。
2. foods：当地特色美食，最多 6 个，每个含 name（美食名）、intro（一句话简介，不超过50字，如风味/吃法）。
只输出 JSON，格式：{{"spots": [{{"name": "...", "intro": "...", "activities": "..."}}], "foods": [{{"name": "...", "intro": "..."}}]}}
不要输出 JSON 以外的任何文字。原文信息不足时宁可少提炼，不要编造。

攻略原文：
{corpus}"""


def _parse_json_block(text: str) -> dict:
    """容忍式解析：截取首个 {...} JSON 块；失败返回空 dict。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def extract_digest_notes(city: str, texts: list[str]) -> dict:
    """LLM 提炼景点/美食笔记；超时/网络/解析任何失败返回空结构（调用方安全降级）。"""
    corpus = "\n".join(t.strip() for t in texts if t and t.strip())[:_MAX_CORPUS_CHARS]
    if not corpus:
        return {"spots": [], "foods": []}
    prompt = _PROMPT.format(city=city or "目的地", corpus=corpus)

    async def _call() -> str:
        result = await get_model_client().create([UserMessage(content=prompt, source="digest")])
        return result.content if isinstance(result.content, str) else ""

    try:
        text = await asyncio.wait_for(_call(), timeout=_LLM_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — 统一容错边界：提炼失败留空走回退
        return {"spots": [], "foods": []}

    data = _parse_json_block(text)
    spots: list[SpotNote] = []
    for item in (data.get("spots") or [])[:8]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        spots.append(SpotNote(name=name[:30],
                              intro=str(item.get("intro") or "").strip()[:60],
                              activities=str(item.get("activities") or "").strip()[:60]))
    foods: list[FoodNote] = []
    for item in (data.get("foods") or [])[:6]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        foods.append(FoodNote(name=name[:30],
                              intro=str(item.get("intro") or "").strip()[:50]))
    return {"spots": spots, "foods": foods}


async def food_image(client: httpx.AsyncClient, name: str, city: str) -> str:
    """美食图：Tavily include_images 取首个可下载非水印图，PIL 校验并重编码
    RGB JPEG（webp 等异格式不进 reportlab），food_ 前缀落盘；失败空串。"""
    if not SearchConfig.TAVILY_API_KEY:
        return ""
    base_name = name.split("（")[0].strip() or name
    r = await client.post("https://api.tavily.com/search", json={
        "api_key": SearchConfig.TAVILY_API_KEY,
        "query": f"{base_name} {city} 美食",
        "max_results": 4,
        "include_images": True,
    })
    r.raise_for_status()
    imgs = r.json().get("images") or []
    urls = [u.get("url") if isinstance(u, dict) else u for u in imgs]
    urls = [u for u in urls if u and not any(h in urlsplit(u).netloc for h in _WATERMARK_HOSTS)]
    for url in urls[:5]:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            if len(resp.content) < 5000:
                continue
            with PILImage.open(io.BytesIO(resp.content)) as im:
                im = im.convert("RGB")
                if im.size[0] < 240 or im.size[1] < 180:
                    continue
                path = IMAGE_DIR / ("food_" + hashlib.md5(f"{name}|{url}".encode()).hexdigest()[:16] + ".jpg")
                im.save(path, quality=88)
            return str(path)
        except Exception:  # noqa: BLE001 — 单候选失败换下一个
            continue
    return ""


async def build_digest_notes(city: str, texts: list[str]) -> dict:
    """提炼 + 搜图一条龙（team.py collect 收尾调用）。全链路防御，失败返回空结构。"""
    notes = await extract_digest_notes(city, texts)
    foods: list[FoodNote] = notes["foods"]
    if foods:
        try:
            async with httpx.AsyncClient(timeout=_IMG_TIMEOUT_S, headers=_IMG_HEADERS,
                                         follow_redirects=True) as client:
                for f in foods:
                    try:
                        f.image_path = await food_image(client, f.name, city)
                    except Exception:  # noqa: BLE001 — 单图失败不拖垮整批
                        f.image_path = ""
        except Exception:  # noqa: BLE001 — 图片批次失败不影响文字笔记
            pass
    return {"spots": notes["spots"], "foods": foods}
