"""酒店适配层（§4.4）：酒店 MCP（社区，覆盖不全）→ 高德距离计算 → 打分勾选 → 降级模拟。"""

from __future__ import annotations

from ..config import ALLOW_MOCK_FALLBACK, McpConfig
from ..mocks.data import kb_for_city, mock_hotels
from .mcp_client import ServiceUnavailable, amap_session, hotel_session
from .resilience import with_retry


async def query_hotels(city: str, location_pref: str | None, price_range: list[float] | None,
                       budget_hint: float | None = None) -> dict:
    """酒店候选查询：真实社区 MCP 优先，未配置/失败降级模拟酒店库（标注参考值）。"""
    notice = None
    candidates: list[dict] = []
    if McpConfig.MCP_HOTEL_URL:
        try:
            session = hotel_session()

            async def _q() -> list:
                return await session.call(("hotel", "search"),
                                          {"city": city, "keyword": location_pref or "", "checkIn": "", "checkOut": ""},
                                          what="酒店查询")

            raw = await with_retry(_q, retries=0, what="酒店查询")
            candidates = _normalize_hotels(raw)
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
    filtered = []
    for c in candidates:
        p = c["price_per_night"]
        if (lo and p < lo) or (hi and p > hi):
            continue
        c["distance_km"] = await _distance_km(city, c["name"], location_pref)
        filtered.append(c)
    if not filtered:  # 区间内无候选时回退全量并提示
        filtered = candidates
        notice = (notice or "") + "｜注意：价格区间内无候选，已放宽为全部候选"
    return {"mode": mode, "notice": notice if notice else None, "candidates": filtered[:5]}


async def _distance_km(city: str, hotel_name: str, landmark: str | None) -> float:
    """酒店距地标距离：高德 MCP 双 POI 坐标 + 哈弗辛距离（真实）；降级为确定性参考值。"""
    lm = landmark or kb_for_city(city)["landmark"]
    if McpConfig.AMAP_API_KEY:
        try:
            session = amap_session()

            async def _poi(keyword: str) -> tuple[float, float] | None:
                raw = await with_retry(
                    lambda: session.call(("text", "search"),
                                         {"keywords": keyword, "city": city, "citylimit": "true"},
                                         what=f"高德 POI 查询（{keyword}）"),
                    retries=0, what="高德 POI 查询")
                pois = (raw or {}).get("pois") if isinstance(raw, dict) else None
                if not pois and isinstance(raw, dict):
                    pois = (raw.get("data", {}) or {}).get("pois")
                for p in pois or []:
                    loc = str(p.get("location", "") or "")
                    if "," in loc:
                        lon, lat = loc.split(",")[:2]
                        return float(lon), float(lat)
                return None

            hotel_pos = await _poi(hotel_name)
            lm_pos = await _poi(lm)
            if hotel_pos and lm_pos:
                return round(_haversine(hotel_pos, lm_pos), 1)
        except (ServiceUnavailable, ValueError):
            pass
    # 降级：确定性参考距离
    import hashlib
    return round(0.3 + int(hashlib.md5(hotel_name.encode()).hexdigest(), 16) % 200 / 100, 1)


def _haversine(a: tuple[float, float], b: tuple[float, float]) -> float:
    """经纬度（度）→ 球面距离 km。"""
    import math
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def _normalize_hotels(raw: object) -> list[dict]:
    rows = raw if isinstance(raw, list) else []
    if isinstance(raw, dict):
        rows = raw.get("hotels") or raw.get("data") or raw.get("result") or []
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
        try:
            rating_f = float(rating) if rating is not None else 4.5
        except (ValueError, TypeError):
            rating_f = 4.5
        out.append({
            "name": str(name), "price_per_night": price_f,
            "distance_km": 1.0, "rating": rating_f,
            "link": str(pick("url", "link") or "https://hotels.ctrip.com"),
            "source": "酒店 MCP（实时数据）", "reference_only": False,
        })
    return out


def score_and_select(candidates: list[dict], price_range: list[float] | None) -> list[dict]:
    """酒店打分勾选（§4.4）：score = 0.4×价格契合度 + 0.3×距离 + 0.3×评分；top1 自动勾选。"""
    if not candidates:
        return []
    dists = [c["distance_km"] for c in candidates] or [1]
    ratings = [c["rating"] for c in candidates] or [4.5]
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
        s = 0.4 * fit + 0.3 * norm(c["distance_km"], d_min, d_max, invert=True) \
            + 0.3 * norm(c["rating"], r_min, r_max, invert=False)
        c["score"] = round(s, 3)
    ranked = sorted(candidates, key=lambda c: -c["score"])
    top = ranked[0]
    top["selected"] = True
    top["reason"] = (f"综合评分最高（{top['score']}）：{top['price_per_night']} 元/晚，距地标 {top['distance_km']}km，"
                     f"评分 {top['rating']}")
    return ranked
