"""酒店适配层（§4.4）：酒店 MCP（社区，覆盖不全）→ 高德距离计算 → 打分勾选 → 降级模拟。

需求 6（2026-08-30）：勾选酒店的宣传图 + 住客评价摘要补充（Tavily 通道，失败留空不阻塞）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from urllib.parse import urlsplit

import httpx

from ..config import ALLOW_MOCK_FALLBACK, IMAGE_DIR, McpConfig, SearchConfig
from ..mocks.data import kb_for_city, mock_hotels
from .mcp_client import ServiceUnavailable, amap_session, hotel_session
from .resilience import with_retry
from .search import _IMG_HEADERS, _IMG_TIMEOUT_S, _WATERMARK_HOSTS

logger = logging.getLogger("tripmate.tools.hotels")


async def enrich_hotels(hotels: list, city: str, top: int = 3) -> None:
    """为已勾选的前 top 家酒店补充宣传图与住客评价摘要（需求 6，2026-08-30）。

    就地写入 HotelCandidate.image_path / review_digest；任一步失败留空，绝不阻塞主流程
    （Tavily 未配置/无结果/下载失败均为正常降级）。
    top 默认 3（2026-09-03）：PDF 酒店卡片改为 3 选展示。"""
    targets = [h for h in hotels if h.selected][:top]
    if not targets or not SearchConfig.TAVILY_API_KEY:
        return
    async with httpx.AsyncClient(timeout=_IMG_TIMEOUT_S, headers=_IMG_HEADERS,
                                 follow_redirects=True) as client:
        # 3 家 ×（图+评价）串行最坏 ~60s 且占住收割路径；sem(2) 控并发防限流
        sem = asyncio.Semaphore(2)

        async def _enrich_one(h) -> None:
            async with sem:
                try:
                    h.image_path = await _hotel_image(client, h.name, city)
                except Exception as e:  # noqa: BLE001 — 宣传图失败不影响评价摘要
                    h.image_path = ""
                    logger.warning("酒店宣传图获取失败「%s」（%s: %s）", h.name, type(e).__name__, e)
                try:
                    h.review_digest = await _hotel_review(client, h.name, city)
                except Exception as e:  # noqa: BLE001 — 评价摘要失败不影响宣传图
                    h.review_digest = ""
                    logger.warning("酒店评价摘要获取失败「%s」（%s: %s）", h.name, type(e).__name__, e)

        await asyncio.gather(*(_enrich_one(h) for h in targets))


async def _hotel_image(client: httpx.AsyncClient, name: str, city: str) -> str:
    """酒店宣传图：Tavily include_images 取首个可下载的非水印候选，落盘 outputs/images。

    品牌名去括号（"全季酒店（成都春熙路店）"→"全季酒店"）：全角括号实测会让检索空手而归。"""
    base_name = name.split("（")[0].strip() or name
    r = await client.post("https://api.tavily.com/search", json={
        "api_key": SearchConfig.TAVILY_API_KEY,
        "query": f"{base_name} {city} 酒店外观".strip(),
        "max_results": 5,
        "include_images": True,
    })
    r.raise_for_status()
    imgs = r.json().get("images") or []
    urls = [u.get("url") if isinstance(u, dict) else u for u in imgs]
    urls = [u for u in urls if u and not any(h in urlsplit(u).netloc for h in _WATERMARK_HOSTS)]
    logger.info("酒店宣传图检索「%s」候选 %d 张", base_name, len(urls))
    for url in urls[:5]:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            if len(resp.content) < 5000:
                continue
            path = IMAGE_DIR / ("hotel_" + hashlib.md5(f"{name}|{url}".encode()).hexdigest()[:16] + ".jpg")
            path.write_bytes(resp.content)
            return str(path)
        except Exception as exc:  # noqa: BLE001 — 单候选失败换下一个
            logger.warning("酒店宣传图下载失败（%s: %s）：%s", type(exc).__name__, exc, urlsplit(url).netloc)
            continue
    logger.warning("酒店宣传图「%s」全部候选不可用", base_name)
    return ""


async def _hotel_review(client: httpx.AsyncClient, name: str, city: str) -> str:
    """住客评价摘要：Tavily include_answer 汇总（≤160 字），标注参考。中文提问保证中文摘要。"""
    base_name = name.split("（")[0].strip() or name
    r = await client.post("https://api.tavily.com/search", json={
        "api_key": SearchConfig.TAVILY_API_KEY,
        "query": f"{base_name} {city} 这家酒店住客评价怎么样？有什么优点和缺点？",
        "max_results": 5,
        "include_answer": True,
    })
    r.raise_for_status()
    answer = (r.json().get("answer") or "").strip()
    return f"{answer[:160]}（网络评价摘要，仅供参考）" if answer else ""


async def query_hotels(city: str, location_pref: str | None, price_range: list[float] | None,
                       budget_hint: float | None = None, dates: list[str] | None = None,
                       party: int = 2) -> dict:
    """酒店候选查询：真实酒店 MCP 优先（Dida），未配置/失败降级模拟酒店库（标注参考值）。
    party 为入住人数（无画像信息时保守沿用历史默认 2 人）。"""
    notice = None
    candidates: list[dict] = []
    party_n = max(1, int(party or 2))
    # Dida 的 price.lowestPrice 是 stayNights 晚的总价，单价须按晚数折算
    nights = max(1, len(dates) - 1) if dates else 1
    if McpConfig.MCP_HOTEL_URL:
        try:
            session = hotel_session()

            async def _q() -> list:
                # Dida searchHotels 嵌套参数；mcp_client 的 schema 过滤只比对顶层键，嵌套 dict 原样透传
                demand = (f"{city} {location_pref} 酒店，{party_n}人入住" if location_pref
                          else f"{city} 酒店，{party_n}人入住")
                args: dict = {"place": city, "placeType": "城市", "originQuery": demand, "size": 8}
                checkin = next((d for d in (dates or []) if len(d) == 10 and d[4] == "-" and d[7] == "-"), "")
                if checkin:
                    args["checkInParam"] = {"checkInDate": checkin,
                                            "stayNights": nights, "adultCount": party_n}
                # 关键词必须唯一切中 searchHotels：getHotelSearchTags 同样含
                # "hotel"+"search" 且在 list_tools 里排在它前面，宽泛关键词会误中
                return await session.call(("searchhotels",), args, what="酒店查询")

            # timeout_s 必须显式传：with_retry 默认 30s 会先于 MCP 90s 硬上限到期，
            # 把仍在正常执行的调用连协程砍掉（npx 冷启动必挂，CALL_TIMEOUT_S 形同虚设）
            raw = await with_retry(_q, retries=1, timeout_s=McpConfig.CALL_TIMEOUT_S, what="酒店查询")
            candidates = _normalize_hotels(raw, nights=nights)
            if not candidates:
                raise ServiceUnavailable("酒店 MCP 返回为空")
        except ServiceUnavailable as e:
            notice = f"酒店 MCP 不可用（{e}），降级模拟酒店库（参考值）"
    if not candidates:
        if not ALLOW_MOCK_FALLBACK and McpConfig.MCP_HOTEL_URL:
            raise ServiceUnavailable("酒店查询失败且不允许降级")
        if not notice:
            notice = "社区酒店 MCP 覆盖不全（未配置），当前为模拟酒店库参考值（§7 分档）"
        candidates = mock_hotels(city, location_pref)
        mode = "mock"
    else:
        mode = "real"

    # 价格区间过滤（用户偏好硬过滤；无偏好时按预算暗示放宽）
    lo, hi = (price_range or [0, 0]) or [0, 0]
    if not (lo or hi) and budget_hint:
        hi = max(300.0, budget_hint * 0.18)
    # 地标坐标整轮只查一次（原先每个候选都重查同一地标：5 家酒店 = 5 次冗余 MCP 会话）
    lm_pos = None
    if McpConfig.AMAP_API_KEY:
        try:
            lm_pos = await _amap_poi(amap_session(), city, location_pref or kb_for_city(city)["landmark"])
        except (ServiceUnavailable, ValueError):
            lm_pos = None
    filtered = []
    for c in candidates:
        pos = c.pop("_pos", None)   # 内部键：Dida 返回的酒店坐标，避免重复查高德
        p = c["price_per_night"]
        if (lo and p < lo) or (hi and p > hi):
            continue
        c["distance_km"], c["distance_estimated"] = await _distance_km(
            city, c["name"], location_pref, hotel_pos=pos, lm_pos=lm_pos)
        filtered.append(c)
    if not filtered:  # 区间内无候选时回退全量并提示
        filtered = candidates
        for c in filtered:
            # 回退候选未经 _distance_km 实算，占位 1km 不得冒充真实距离参与归一/展示（§7 估算标注）
            c.setdefault("distance_estimated", True)
        notice = (notice or "") + "｜注意：价格区间内无候选，已放宽为全部候选"
    return {"mode": mode, "notice": notice if notice else None, "candidates": filtered[:5]}


async def _amap_poi(session, city: str, keyword: str) -> tuple[float, float] | None:
    """高德 POI 坐标查询：text_search → 行内坐标缺失时按 id 走 search_detail 补齐。

    实测（2026-09-03）：amap MCP 的 text_search 行只有 id/name/address/typecode/photo，
    坐标需按 id 走 search_detail 补齐。session 按调用传入（McpSession.call 每次自带连接生命周期）。"""
    raw = await with_retry(
        lambda: session.call(("text", "search"),
                             {"keywords": keyword, "city": city, "citylimit": "true"},
                             what=f"高德 POI 查询（{keyword}）"),
        retries=1, timeout_s=McpConfig.CALL_TIMEOUT_S, what="高德 POI 查询")
    pois = (raw or {}).get("pois") if isinstance(raw, dict) else None
    if not pois and isinstance(raw, dict):
        pois = (raw.get("data", {}) or {}).get("pois")
    for p in pois or []:
        loc = str(p.get("location", "") or "")
        if "," in loc:
            lon, lat = loc.split(",")[:2]
            return float(lon), float(lat)
    for p in pois or []:
        pid = str(p.get("id", "") or "")
        if not pid:
            continue
        raw_d = await with_retry(
            lambda pid=pid: session.call(("detail",), {"id": pid},
                                         what=f"高德 POI 详情（{keyword}）"),
            retries=1, timeout_s=McpConfig.CALL_TIMEOUT_S, what="高德 POI 详情")
        detail = raw_d if isinstance(raw_d, dict) else {}
        loc = str(detail.get("location", "") or "")
        if "," in loc:
            lon, lat = loc.split(",")[:2]
            return float(lon), float(lat)
    return None


async def _distance_km(city: str, hotel_name: str, landmark: str | None,
                       hotel_pos: tuple[float, float] | None = None,
                       lm_pos: tuple[float, float] | None = None) -> tuple[float, bool]:
    """酒店距地标距离：优先用 MCP 自带的酒店坐标（省一次 POI 查询），高德补地标坐标 + 哈弗辛（真实）；
    降级为确定性参考值。返回 (距离, 是否估算)；坐标口径 (lon, lat)。
    lm_pos 由调用方传入（query_hotels 整轮只查一次地标）。"""
    lm = landmark or kb_for_city(city)["landmark"]
    if McpConfig.AMAP_API_KEY:
        try:
            session = amap_session()
            if hotel_pos is None:
                hotel_pos = await _amap_poi(session, city, hotel_name)
            if lm_pos is None:
                lm_pos = await _amap_poi(session, city, lm)
            if hotel_pos and lm_pos:
                return round(_haversine(hotel_pos, lm_pos), 1), False
        except (ServiceUnavailable, ValueError) as e:
            # 此前静默吞掉：用户无从知晓距离是编的（配合 estimated 标注才成立）
            logger.warning("高德距离计算失败「%s」（%s: %s），降级为估算参考值", hotel_name,
                           type(e).__name__, e)
    # 降级：确定性参考距离
    return round(0.3 + int(hashlib.md5(hotel_name.encode()).hexdigest(), 16) % 200 / 100, 1), True


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """经纬度（度）→ 球面距离 km。"""
    import math
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _normalize_dida(rows: list, nights: int) -> list[dict]:
    """Dida searchHotels 行 → HotelCandidate 字段（键集须与模型一致）。
    price.lowestPrice 为 stayNights 晚总价；坐标随行返回，内部键 _pos 供距离计算复用。"""
    out = []
    for r in rows[:10]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("name") or "").strip()
        price = r.get("price") or {}
        if not name or not isinstance(price, dict) or not price.get("hasPrice", True):
            continue
        try:
            total = float(price.get("lowestPrice") or 0)
        except (TypeError, ValueError):
            continue
        if total <= 0:
            continue
        try:
            rating = float(r.get("starRating") or 0)
        except (TypeError, ValueError):
            rating = 0.0
        try:
            pos = (float(r.get("longitude")), float(r.get("latitude")))
        except (TypeError, ValueError):
            pos = None
        item = {
            "name": name,
            "price_per_night": round(total / max(1, nights), 1),
            "distance_km": 1.0,
            "rating": rating if rating > 0 else 4.5,
            "rating_estimated": rating <= 0,   # Dida 无评分时的 4.5 是兜底值，不得冒充实评
            "link": str(r.get("bookingUrl") or ""),
            "source": "Dida 酒店 MCP（实时数据）",
            "reference_only": False,
        }
        if pos:
            item["_pos"] = pos
        out.append(item)
    return out


def _normalize_hotels(raw: object, nights: int = 1) -> list[dict]:
    """酒店候选解析：Dida MCP（hotelInformationList，price.lowestPrice 为 stayNights 晚总价）
    优先；其他社区实现沿用字段名模糊匹配。"""
    if isinstance(raw, dict) and isinstance(raw.get("hotelInformationList"), list):
        return _normalize_dida(raw["hotelInformationList"], nights)
    rows = raw if isinstance(raw, list) else []
    if isinstance(raw, dict):
        rows = raw.get("hotels") or raw.get("data") or raw.get("result") or []
    if isinstance(rows, dict):
        rows = [rows]  # 单对象包装成列表（与 tickets 同型防御），防 rows[:10] TypeError 绕过 mock 降级
    elif not isinstance(rows, list):
        rows = []
    out = []
    for r in rows[:10]:
        if not isinstance(r, dict):
            continue
        def pick(*keys: str):
            for k in keys:
                for rk, rv in r.items():
                    if k.lower() in rk.lower() and rv not in (None, ""):
                        return rv
            return None
        name = pick("name", "hotelName", "title")
        price = pick("price", "avgPrice", "startPrice")
        if not name or price is None:
            continue
        try:
            price_f = float(str(price).replace("¥", "").replace(",", ""))
        except ValueError:
            continue
        rating = pick("rating", "score", "star")
        rating_estimated = False
        try:
            rating_f = float(rating) if rating is not None else 4.5
            rating_estimated = rating is None
        except (ValueError, TypeError):
            rating_f = 4.5
            rating_estimated = True
        out.append({
            "name": str(name), "price_per_night": price_f,
            "distance_km": 1.0, "rating": rating_f,
            "rating_estimated": rating_estimated,
            "link": str(pick("url", "link") or "https://hotels.ctrip.com"),
            "source": "酒店 MCP（实时数据）", "reference_only": False,
        })
    return out


def score_and_select(candidates: list[dict], price_range: list[float] | None) -> list[dict]:
    """酒店打分勾选（§4.4）：score = 0.4×价格契合度 + 0.3×距离 + 0.3×评分；top1 自动勾选。

    估算字段（distance_estimated/rating_estimated）不参与 min/max 归一、按中性 0.5 权重计——
    此前哈希伪距离/兜底评分与真实值同池归一，混入"实时数据"影响排序（2026-09-05）。"""
    if not candidates:
        return []
    real_dists = [c["distance_km"] for c in candidates if not c.get("distance_estimated")]
    real_ratings = [c["rating"] for c in candidates if not c.get("rating_estimated")]
    dists = real_dists or [c["distance_km"] for c in candidates] or [1]
    ratings = real_ratings or [c["rating"] for c in candidates] or [4.5]
    d_min, d_max = min(dists), max(dists)
    r_min, r_max = min(ratings), max(ratings)
    lo, hi = (price_range or [0, 0]) or [0, 0]

    def norm(v: float, lo_v: float, hi_v: float, invert: bool) -> float:
        if hi_v <= lo_v:
            return 1.0
        t = (v - lo_v) / (hi_v - lo_v)
        return 1 - t if invert else t

    for c in candidates:
        if lo and hi and lo <= c["price_per_night"] <= hi:
            fit = 1.0
        elif lo or hi:
            mid = (lo + hi) / 2 if (lo and hi) else (lo or hi)
            fit = max(0.2, 1 - abs(c["price_per_night"] - mid) / max(mid, 1))
        else:
            fit = 0.8
        d_comp = 0.5 if c.get("distance_estimated") else norm(c["distance_km"], d_min, d_max, invert=True)
        r_comp = 0.5 if c.get("rating_estimated") else norm(c["rating"], r_min, r_max, invert=False)
        s = 0.4 * fit + 0.3 * d_comp + 0.3 * r_comp
        c["score"] = round(s, 3)
    ranked = sorted(candidates, key=lambda c: -c["score"])
    top = ranked[0]
    top["selected"] = True
    top["reason"] = (f"综合评分最高（{top['score']}）：{top['price_per_night']} 元/晚，距地标 {top['distance_km']}km，"
                     f"评分 {top['rating']}")
    est = [label for flag, label in ((top.get("distance_estimated"), "距离"), (top.get("rating_estimated"), "评分")) if flag]
    if est:
        top["reason"] += f"（{'/'.join(est)}为估算参考值）"
    return ranked
