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
import logging
import re
from urllib.parse import urlsplit

import httpx
from PIL import Image as PILImage
from autogen_core.models import UserMessage

from .config import IMAGE_DIR, SearchConfig
from .llm import get_model_client
from .models import AlarmItem, DayTheme, FoodNote, GuideExtras, SpotNote, TravelProfile
from .tools.search import _IMG_HEADERS, _WATERMARK_HOSTS

logger = logging.getLogger("tripmate.digest")

_LLM_TIMEOUT_S = 45.0    # 提炼单次上限：collect 收尾同步等待，超时即放弃走回退
_STRUCT_TIMEOUT_S = 300.0  # 攻略结构化单次上限：晚间免费档拥堵实测 150s 仍不够（2026-09-19，与备通道 300s 同理——
                           # 拥堵窗口砍死本可完成的调用）；跑在 JobBoard 900s 预算内，放宽安全
_EXTRAS_TIMEOUT_S = 150.0  # 路书锦囊上限：45s 实测被吃满截断（2026-09-19 e2e：提炼耗时 46s → 闹钟/主题全空）；
                           # 在群聊轮内执行，取 150s 平衡拥堵与定稿时延
_IMG_TIMEOUT_S = 12.0
_MAX_CORPUS_CHARS = 6000

# 工具型 JSON 提炼统一降思考力度（2026-09-19 e2e 根因：glm-5.3-flash 思考 token 计入
# max_tokens，重提示词下推理耗尽预算 → content 为空/结构化全空/群聊消息被清洗成占位；
# effort low 实测 8/8 行正常产出，且不再出现空 content）。规划类 Agent 不套用，保留深度思考。
# max_tokens 16384（2026-09-19 实测通过：autogen 路径接受、正常产出）——拥堵期推理变长时
# 给正文留余量，健康期模型自行 stop 不受影响。
_LLM_JSON_ARGS = {"extra_create_args": {"extra_body": {"thinking": {"type": "enabled", "effort": "low"}},
                                        "max_tokens": 16384}}

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
        result = await get_model_client().create([UserMessage(content=prompt, source="digest")],
                                        **_LLM_JSON_ARGS)
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


_STRUCT_PROMPT = """你是旅行攻略编辑。下面是关于「{city}」的攻略搜索摘要（编号条目，每条含来源、标题与原文片段）。
请逐条提炼结构化信息，只输出 JSON：{{"rows": [条目1, 条目2, ...]}}，rows 与输入条目一一对应（顺序、条数一致），
每条格式：{{"spots": ["具体景点名"], "foods": ["具体美食名"], "routes": ["完整动线：A→B→C"], "warnings": ["避坑提示"]}}
硬约束：spots/foods 名称一律用中文原文，不得中英混排或翻译，不得编造、不得保留整句标题；
routes/warnings 必须来自原文；原文没有的信息一律给空数组。不要输出 JSON 以外的任何文字。

攻略原文：
{corpus}"""

_STRUCT_ROW_CHARS = 1200   # 每路原文片段截断：8 路 ≈ 10K 字符语料，输出 8 条结构化 ≈ 2-3K tokens


def _bigram_hits(name: str, text: str) -> bool:
    """名称软校验：与原文双向子串，或名称任一 2-gram 出现在原文中（容忍"熊猫基地"式合法简称，
    同时挡住与原文零字符重叠的编造项）。"""
    if not name or not text:
        return False
    if name in text:
        return True
    return any(name[i:i + 2] in text for i in range(max(len(name) - 1, 1)))


def _parse_rows_array(text: str) -> list | None:
    """容忍式解析 rows 数组：优先 {"rows": [...]}；失败则剥 markdown 围栏后尝试裸 JSON 数组。"""
    data = _parse_json_block(text).get("rows")
    if isinstance(data, list):
        return data
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    if stripped.startswith("["):
        try:
            arr = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if isinstance(arr, list):
            return arr
    return None


async def structure_guide_rows(rows: list[dict], city: str) -> list[dict]:
    """攻略原始行结构化（下沉在搜索通道内完成，Researcher 不再在群聊里整理）。

    背景（2026-09-19 根因）：原设计要求 Researcher 把 raw_answer/标题整理为 digest_json 提交，
    但原始内容只在 subagent 通道内部流转、从不进入其模型上下文——指令等于要求模型整理一份
    它从未见过的数据，结构化字段因此常年为空、由护栏重复提炼。
    全链路防御：超时/网络/解析/校验任何失败原样返回 rows，绝不向上抛（否则会被
    run_channel_subagent 的重试+降级链放大成多次查询）；mock 通道行已自带结构化四元，直接跳过。
    所有回退路径记 WARNING（2026-09-19 e2e 教训：静默回退曾让结构化失败完全不可见）。
    """
    texts = []
    for i, d in enumerate(rows, 1):
        answer = str(d.get("raw_answer") or "").strip()
        titles = "；".join(t for t in (d.get("raw_titles") or []) if t)
        if not answer and not titles:
            continue
        texts.append(f"【{i}】{d.get('source_name', '')}\n标题：{titles}\n原文：{answer[:_STRUCT_ROW_CHARS]}")
    corpus = "\n".join(texts)
    if not corpus:
        logger.info("攻略结构化跳过：无原文语料（mock 通道行已自带结构化四元）")
        return rows
    prompt = _STRUCT_PROMPT.format(city=city or "目的地", corpus=corpus[:12000])

    async def _call() -> str:
        result = await get_model_client().create([UserMessage(content=prompt, source="digest")],
                                        **_LLM_JSON_ARGS)
        return result.content if isinstance(result.content, str) else ""

    try:
        text = await asyncio.wait_for(_call(), timeout=_STRUCT_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — 统一容错边界：整理失败原样回退，调用方与降级链零感知
        logger.warning("攻略结构化 LLM 调用失败（%s: %s），回退原始行", type(exc).__name__, exc)
        return rows
    data = _parse_rows_array(text)
    if not isinstance(data, list) or not data:
        logger.warning("攻略结构化解析失败（未取得 rows 数组，输出前 120 字符：%r），回退原始行",
                       (text or "")[:120])
        return rows

    out = [dict(d) for d in rows]
    for row, item in zip(out, data):
        if not isinstance(item, dict):
            continue
        raw_text = " ".join(filter(None, (str(row.get("raw_answer") or ""),
                                          " ".join(row.get("raw_titles") or []))))
        for key, cap, n_max in (("spots", 30, 8), ("foods", 30, 6),
                                ("routes", 100, 3), ("warnings", 80, 5)):
            row[key] = [v for v in (str(x).strip()[:cap] for x in (item.get(key) or [])[:n_max])
                        if v and _bigram_hits(v, raw_text)]
    # 全部条目均未产出任何结构化字段（如解析到无关 JSON）：视为未产出，原样回退
    if not any(row.get(k) for row in out for k in ("spots", "foods", "routes", "warnings")):
        logger.warning("攻略结构化校验后全空（软校验过滤了全部条目），回退原始行")
        return rows
    filled = sum(1 for row in out if any(row.get(k) for k in ("spots", "foods", "routes", "warnings")))
    logger.info("攻略结构化完成：%d/%d 行产出结构化字段", filled, len(out))
    return out


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


_EXTRAS_PROMPT = """你是旅行路书主编。根据下面的行程素材，为「{origin}→{destination} {days}天旅行路书」提炼：
1. overview_intro：速览页引言，两三句话讲清"为什么这么排"（动线/光线/错峰逻辑，不超过120字）。
2. day_themes：每天的 theme（当日主题，不超过12字，如"中轴皇城"）和 line（一条线讲完的当日动线，用"→"串联主要点位，不超过80字）。天数：{days} 天，按天序输出。
3. alarms：出发前必须设的抢票/预约闹钟，按时间倒排，最多 6 条，每条含 when（时间点，如"今天 · 立即"/"9月27日 20:00"）、action（动作，含票种与数量口径）、channel（渠道与要领，如"12306；提前15天整点放票"）、difficulty（难度：🔴 极难 / 🟡 中 / ⭐ 优先 之一）。
4. hotel_verdict：住宿"三选一定稿"的推荐理由（为什么主推 A，两句话以内，不超过100字）。
5. rhythm_note：全程节奏说明（体力分配/收工时间等，一句话，不超过80字）。
只输出 JSON，格式：{{"overview_intro": "...", "day_themes": [{{"theme": "...", "line": "..."}}], "alarms": [{{"when": "...", "action": "...", "channel": "...", "difficulty": "..."}}], "hotel_verdict": "...", "rhythm_note": "..."}}
不要输出 JSON 以外的任何文字。素材信息不足的字段宁可留空字符串/空数组，不要编造。

行程素材：
{corpus}"""


def _extras_corpus(profile: TravelProfile) -> str:
    """TravelProfile → LLM 提炼用紧凑素材（纯既有字段拼接，不额外消耗调用）。"""
    basic, draft = profile.basic_info, profile.draft
    parts: list[str] = []
    parts.append(f"出发地：{basic.origin or '待定'}；目的地：{basic.destination or '待定'}；"
                 f"{basic.days or '-'} 天；日期：{'、'.join(basic.travel_dates) or basic.date_text or '待定'}；"
                 f"风格：{'/'.join(basic.style) or '休闲'}；人数：{detail_party(profile)} 人")
    must_visit = "、".join(profile.detail_info.must_visit[:6])
    if must_visit:
        parts.append(f"必去：{must_visit}")
    if profile.tickets:
        tks = ["；".join(filter(None, (t.train_no, t.depart_time, t.arrive_time,
                                       f"{t.price:g}元", "已选" if t.selected else "")))
               for t in profile.tickets[:6]]
        parts.append("车票候选：" + " ｜ ".join(tks))
    if profile.hotels:
        hts = ["；".join(filter(None, (h.name, f"{h.price_per_night:g}元/晚",
                                       f"评分{h.rating:g}", "已选" if h.selected else "", h.reason[:40])))
               for h in profile.hotels[:3]]
        parts.append("酒店候选：" + " ｜ ".join(hts))
    if draft:
        for i, d in enumerate(draft.days, 1):
            seg = "；".join(filter(None, (
                f"上午 {d.morning[:40]}" if d.morning else "",
                f"下午 {d.afternoon[:40]}" if d.afternoon else "",
                f"晚上 {d.evening[:40]}" if d.evening else "")))
            parts.append(f"D{i} {'/'.join(d.spots[:4])}：{seg}")
    return "\n".join(parts)[:_MAX_CORPUS_CHARS]


def detail_party(profile: TravelProfile) -> int:
    """同行人数（与 pdf_html/context 同口径，独立小函数避免反向依赖模板层）。"""
    return profile.detail_info.party_size or profile.basic_info.party_size or 1


async def build_guide_extras(profile: TravelProfile) -> GuideExtras:
    """定稿前的"路书锦囊"提炼（team._deliver_final 调用）：速览引言/每日主题/抢约闹钟/
    住宿定稿理由/节奏说明。限时+容忍解析，任何失败返回空 GuideExtras（PDF 走通用回退）。"""
    basic = profile.basic_info
    corpus = _extras_corpus(profile)
    if not (basic.destination and profile.draft):
        return GuideExtras()
    prompt = _EXTRAS_PROMPT.format(origin=basic.origin or "出发地", destination=basic.destination,
                                   days=basic.days or "-", corpus=corpus)

    async def _call() -> str:
        result = await get_model_client().create([UserMessage(content=prompt, source="digest")],
                                        **_LLM_JSON_ARGS)
        return result.content if isinstance(result.content, str) else ""

    try:
        text = await asyncio.wait_for(_call(), timeout=_EXTRAS_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — 统一容错边界：提炼失败留空走回退
        logger.warning("路书锦囊提炼失败（%s: %s），PDF 走通用回退", type(exc).__name__, exc)
        return GuideExtras()

    data = _parse_json_block(text)
    themes = [DayTheme(theme=str(t.get("theme") or "").strip()[:12],
                       line=str(t.get("line") or "").strip()[:80])
              for t in (data.get("day_themes") or [])[:10] if isinstance(t, dict)]
    alarms = [AlarmItem(when=str(a.get("when") or "").strip()[:24],
                        action=str(a.get("action") or "").strip()[:60],
                        channel=str(a.get("channel") or "").strip()[:60],
                        difficulty=str(a.get("difficulty") or "").strip()[:8])
              for a in (data.get("alarms") or [])[:6] if isinstance(a, dict)]
    return GuideExtras(
        overview_intro=str(data.get("overview_intro") or "").strip()[:120],
        day_themes=[t for t in themes if t.theme or t.line],
        alarms=[a for a in alarms if a.when or a.action],
        hotel_verdict=str(data.get("hotel_verdict") or "").strip()[:100],
        rhythm_note=str(data.get("rhythm_note") or "").strip()[:80],
    )
