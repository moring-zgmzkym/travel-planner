"""路线规划适配层（route 通道）：高德 MCP 每日路线计算——景点坐标、段间距离/耗时/交通、
饭点餐厅推荐（结合美食板块菜名与饮食禁忌）与"从当前位置出发"的导航链接。

设计约束（2026-09-06 四轮风险审查定稿）：
- 全确定性、可单测：MCP 交互集中在小函数，session_factory 可注入测试桩（无网）；
- 总预算护栏（RouteConfig.BUDGET_S）：单调时钟超限后剩余段直接切哈弗辛离线估算并标注
  参考值——一次计算含坐标/段间/饭点共 20-35 次串行 MCP 调用，单次各有 90s 硬上限，
  无总量约束时最坏可拖 30+ 分钟（2026-09-04 collect 停摆事故的同型风险）；
- 逐级降级、绝不抛出：段间 MCP direction → 哈弗辛估算；饭点 around_search → text_search
  → "自由安排"；任何失败只降级当前段并记日志，绝不阻塞 PDF；
- 导航链接省略 from 参数 = 高德运行时取 GPS 当前位置，即"当前位置 → 目标站"一键导航，
  系统无需（也无法）在行中追踪用户位置。
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

from autogen_core.models import UserMessage

from ..config import McpConfig, RouteConfig
from ..models import RouteDay, RouteSegment, RouteStop, TravelProfile
from ..mocks.data import kb_for_city
from .hotels import _haversine

logger = logging.getLogger("tripmate.tools.route")

_WALK_MAX_M = 1500        # 哈弗辛预判：直线距离低于此值步行，否则公交
_TRANSIT_MAX_M = 25000    # 高于此值驾车并附跨城警告
_MEAL_RADIUS_M = 2000     # 饭点餐厅周边搜索半径
_WALK_KMH, _TRANSIT_KMH, _DRIVE_KMH = 4.0, 20.0, 30.0   # 离线估算速度
_REVIEW_TIMEOUT_S = 45.0  # LLM 审查单次上限（digest.py 同款）

_URI_TMPL = ("https://uri.amap.com/navigation?to={to}&mode={mode}"
             "&src=tripmate&coordinate=gaode&callnative=0")
_URI_MODE = {"步行": "walk", "公交": "bus", "驾车": "car"}
_DIRECTION_KEYWORDS = {"步行": ("direction", "walking"),
                       "公交": ("direction", "transit"),
                       "驾车": ("direction", "driving")}
_EST_SPEED = {"步行": _WALK_KMH, "公交": _TRANSIT_KMH, "驾车": _DRIVE_KMH}


def _nav_url(lon: float, lat: float, name: str, mode: str) -> str:
    """高德 URI 导航链接。name 整体 quote（safe=''，防名称含 &/逗号截断参数）；
    lon/lat 间的逗号保持字面量。省略 from = 运行时取 GPS 当前位置。"""
    to = f"{lon},{lat},{quote(name or '', safe='')}"
    return _URI_TMPL.format(to=to, mode=_URI_MODE.get(mode, "bus"))


def _strip_paren(name: str) -> str:
    """去括号别称（"陕西历史博物馆（陕历博）"→"陕西历史博物馆"），全角/半角都处理。"""
    base = (name or "").split("（")[0].split("(")[0].strip()
    return base or (name or "").strip()


def _to_f(v) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


def _pois(raw) -> list:
    """高德 MCP 返回 → pois 列表（text_search/around_search 同构，兼容 data 包装）。"""
    if not isinstance(raw, dict):
        return []
    pois = raw.get("pois")
    if not pois and isinstance(raw.get("data"), dict):
        pois = raw["data"].get("pois")
    return pois if isinstance(pois, list) else []


def _poi_loc(p) -> tuple[float, float] | None:
    loc = str(p.get("location", "") or "") if isinstance(p, dict) else ""
    if "," in loc:
        try:
            lon, lat = (float(x) for x in loc.split(",")[:2])
            return lon, lat
        except ValueError:
            return None
    return None


class _Budget:
    """路线计算总预算（单调时钟）：每次 MCP 调用前检查，超限即切换离线估算。"""

    def __init__(self) -> None:
        self._t0 = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self._t0 >= RouteConfig.BUDGET_S


async def _geocode(session, budget: _Budget, cache: dict, name: str, city: str):
    """名称 → ((lon, lat), address) | None。跨天同名缓存；deadline 内未查到缓存 None 防重复尝试。
    自持 POI 查询（text_search 行自带 address，坐标缺失按 id 走 search_detail 补齐）——
    hotels._amap_poi 只回坐标，路线表还需要地址小字（2026-09-06 e2e 视觉验收缺口）。"""
    if name in cache:
        return cache[name]
    if session is None or budget.expired():
        cache[name] = None
        return None
    pos = None
    address = ""
    try:
        raw = await session.call(("text", "search"),
                                 {"keywords": _strip_paren(name), "city": city,
                                  "citylimit": "true"},
                                 what=f"路线 POI（{name}）")
        pois = _pois(raw)
        for p in pois:  # 行内带坐标的首选（省一次 detail）
            loc = _poi_loc(p)
            if loc is not None:
                pos, address = loc, str(p.get("address", "") or "").strip()
                break
        if pos is None:
            for p in pois:
                pid = str(p.get("id", "") or "")
                if not pid:
                    continue
                raw_d = await session.call(("detail",), {"id": pid},
                                           what=f"路线 POI 详情（{name}）")
                d = raw_d if isinstance(raw_d, dict) else {}
                if isinstance(d.get("data"), dict):
                    d = d["data"]
                loc = _poi_loc(d)
                if loc is not None:
                    pos = loc
                    address = str(d.get("address", "") or "").strip()
                    break
    except Exception as exc:  # noqa: BLE001 — 单点失败不拖垮整表
        logger.warning("路线坐标查询失败「%s」（%s: %s）", name, type(exc).__name__, exc)
        pos = None
    cache[name] = (pos, address) if pos else None
    return cache[name]


def _pick_restaurant(pois: list, restrictions: list[str]) -> tuple[str, str, str] | None:
    """周边 POI → 首个名称不含禁忌关键词的餐厅。返回 (name, address, poi_id) | None。
    周边返回按距中心点近远排序（高德默认），且 POI 行不带坐标（与 text_search 同构），
    距离比较与导航坐标由调用方按 id 走 search_detail 补齐。"""
    banned = [r for r in restrictions if r]
    for p in pois:
        name = str(p.get("name", "") or "").strip()
        if not name or any(b in name for b in banned):
            continue
        return name, str(p.get("address", "") or "").strip(), str(p.get("id", "") or "")
    return None


async def _poi_coords(session, budget: _Budget, city: str, name: str,
                      pid: str) -> tuple[float, float] | None:
    """餐厅坐标：search_detail（按 id，最准）→ text_search（按名）→ None。"""
    if session is None or budget.expired():
        return None
    if pid:
        try:
            raw = await session.call(("detail",), {"id": pid}, what=f"餐厅坐标（{name}）")
            loc = _poi_loc(raw if isinstance(raw, dict) else {})
            if loc is None and isinstance(raw, dict) and isinstance(raw.get("data"), dict):
                loc = _poi_loc(raw["data"])
            if loc is not None:
                return loc
        except Exception as exc:  # noqa: BLE001 — 换 text_search 兜底
            logger.warning("餐厅详情坐标失败「%s」（%s: %s）", name, type(exc).__name__, exc)
    geo = await _geocode(session, budget, {}, name, city)
    return geo[0] if geo else None


async def _meal_stop(session, budget: _Budget, city: str, anchor: tuple[float, float] | None,
                     meal: str, dish: str, restrictions: list[str]) -> RouteStop | None:
    """饭点餐厅：around_search（锚点周边+餐饮类别）→ text_search（城市+菜名）→ 自由安排。"""
    if session is None or budget.expired() or anchor is None:
        return None
    raw = None
    try:
        raw = await session.call(("around",),
                                 {"location": f"{anchor[0]},{anchor[1]}", "keywords": dish,
                                  "radius": str(_MEAL_RADIUS_M)},
                                 what=f"饭点餐厅（{meal}·{dish}）")
    except Exception as exc:  # noqa: BLE001 — 降级 text_search
        logger.warning("饭点周边搜索失败（%s·%s，%s: %s），降级关键词搜索", meal, dish,
                       type(exc).__name__, exc)
    pick = _pick_restaurant(_pois(raw), restrictions)
    if pick is None:
        try:
            raw = await session.call(("text", "search"),
                                     {"keywords": f"{city} {dish}", "city": city,
                                      "citylimit": "true"},
                                     what=f"饭点餐厅（{meal}·{dish}）")
            pick = _pick_restaurant(_pois(raw), restrictions)
        except Exception as exc:  # noqa: BLE001 — 降级自由安排
            logger.warning("饭点关键词搜索失败（%s·%s，%s: %s）", meal, dish,
                           type(exc).__name__, exc)
    if pick is None:
        return None
    name, addr, pid = pick
    loc = await _poi_coords(session, budget, city, name, pid)
    stop = RouteStop(kind="meal", meal=meal, name=name, address=addr)
    if loc is not None:
        stop.lon, stop.lat = loc
        stop.nav_url = _nav_url(loc[0], loc[1], name, "公交")
    else:
        stop.note = "坐标未获取"
    return stop


def _pick_mode(straight_m: float) -> tuple[str, bool]:
    if straight_m < _WALK_MAX_M:
        return "步行", False
    if straight_m <= _TRANSIT_MAX_M:
        return "公交", False
    return "驾车", True


def _parse_direction(raw, mode: str) -> RouteSegment | None:
    """高德 direction 返回 → RouteSegment（防御性提取，解析不出返回 None）。
    实测（2026-09-06）：步行/驾车包在 route.paths[]（distance/duration 为数值）；
    公交字段在顶层（distance/duration 为字符串，无 route 包装）——两种形态都兼容。"""
    if not isinstance(raw, dict):
        return None
    if mode == "公交":
        route = raw if isinstance(raw.get("transits"), list) else raw.get("route")
        if not isinstance(route, dict):
            return None
        durs = [_to_f(t.get("duration")) for t in (route.get("transits") or [])
                if isinstance(t, dict) and _to_f(t.get("duration")) > 0]
        if not durs:
            return None
        return RouteSegment(distance_m=int(_to_f(route.get("distance"))),
                            duration_min=max(1, round(min(durs) / 60)), mode=mode)
    route = raw.get("route")
    if not isinstance(route, dict) and isinstance(raw.get("data"), dict):
        route = raw["data"].get("route")
    if not isinstance(route, dict):
        return None
    for p in (route.get("paths") or []):
        if not isinstance(p, dict):
            continue
        dist, dur = _to_f(p.get("distance")), _to_f(p.get("duration"))
        if dist > 0 or dur > 0:
            return RouteSegment(distance_m=int(dist),
                                duration_min=max(1, round(dur / 60)) if dur > 0 else 0, mode=mode)
    return None


async def _segment(session, budget: _Budget, a: tuple[float, float], b: tuple[float, float],
                   city: str) -> tuple[RouteSegment, bool]:
    """段间计算：哈弗辛预判交通方式 → MCP direction → 失败/超时切离线估算。返回 (段, 是否跨城警告)。"""
    straight_m = _haversine(a, b) * 1000
    mode, cross = _pick_mode(straight_m)
    if session is not None and not budget.expired():
        args = {"origin": f"{a[0]},{a[1]}", "destination": f"{b[0]},{b[1]}"}
        if mode == "公交":
            args["city"] = city
        try:
            raw = await session.call(_DIRECTION_KEYWORDS[mode], args, what=f"段间路线（{mode}）")
            seg = _parse_direction(raw, mode)
            if seg is not None:
                return seg, cross
        except Exception as exc:  # noqa: BLE001 — 降级离线估算
            logger.warning("段间路线查询失败（%s，%s: %s），切离线估算", mode,
                           type(exc).__name__, exc)
    dur_min = max(1, round(straight_m / 1000 / _EST_SPEED[mode] * 60))
    return RouteSegment(distance_m=round(straight_m), duration_min=dur_min, mode=mode,
                        reference_only=True), cross


async def _day_route(session, budget: _Budget, cache: dict, day, day_index: int, city: str,
                     dishes: list[str], restrictions: list[str],
                     hotel_pos: tuple[float, float] | None) -> RouteDay:
    """单日路线：景点坐标 → 中点切分插午餐（锚=前半段末景点）→ 末尾插晚餐（锚=酒店/末景点）
    → 相邻站段间。任何一站坐标缺失仅影响相邻段（该段置空），不拖垮当日其余路线。"""
    names = [s for s in (day.spots or []) if s and s.strip()]
    warnings: list[str] = []
    if not names:
        return RouteDay(date=day.date, warnings=["当日无景点安排，路线略"])

    entries: list[tuple[RouteStop, tuple[float, float] | None]] = []
    for n in names:
        geo = await _geocode(session, budget, cache, n, city)
        pos = geo[0] if geo else None
        stop = RouteStop(kind="spot", name=n, address=geo[1] if geo else "",
                         note="" if pos else "坐标未获取")
        if pos:
            stop.lon, stop.lat = pos
            stop.nav_url = _nav_url(pos[0], pos[1], n, "公交")
        entries.append((stop, pos))

    # 午餐：中点切分（3 景点 → 第 2 站后），锚 = 前半段最后一个有坐标景点
    split = (len(entries) + 1) // 2
    lunch_anchor = next((c for c in (e[1] for e in entries[:split][::-1]) if c), None)
    lunch = None
    if lunch_anchor is not None and not budget.expired():
        dish_l = dishes[day_index % len(dishes)] if dishes else "餐厅"
        lunch = await _meal_stop(session, budget, city, lunch_anchor, "午餐", dish_l, restrictions)
    if lunch is not None:
        entries.insert(split, (lunch, (lunch.lon, lunch.lat)))
    else:
        entries.insert(split, (RouteStop(kind="meal", meal="午餐", name="午餐：自由安排",
                                         note="未找到合适餐厅（可导航至附近商圈）", reference_only=True), None))

    # 晚餐：末尾追加，锚 = 勾选酒店（顺路回酒店）→ 末景点
    dinner_anchor = hotel_pos or next((c for c in (e[1] for e in entries[::-1]) if c), None)
    dinner = None
    if dinner_anchor is not None and not budget.expired():
        dish_d = dishes[(day_index + 1) % len(dishes)] if dishes else "餐厅"
        dinner = await _meal_stop(session, budget, city, dinner_anchor, "晚餐", dish_d, restrictions)
    if dinner is not None:
        entries.append((dinner, (dinner.lon, dinner.lat)))
    else:
        entries.append((RouteStop(kind="meal", meal="晚餐", name="晚餐：自由安排",
                                  note="未找到合适餐厅（可导航至附近商圈）", reference_only=True), None))

    segments: list[RouteSegment] = []
    for i in range(len(entries) - 1):
        ca, cb = entries[i][1], entries[i + 1][1]
        if ca is None or cb is None:
            segments.append(RouteSegment())  # 端点缺坐标：空段占位，保持与 stops 对齐
            continue
        seg, cross = await _segment(session, budget, ca, cb, city)
        segments.append(seg)
        if cross:
            stops = entries[i][0], entries[i + 1][0]
            warnings.append(f"{stops[0].name} → {stops[1].name} 距离较远（跨城/长距离），建议城际交通或调整顺序")

    total_km = round(sum(s.distance_m for s in segments if s.distance_m > 0) / 1000, 1)
    return RouteDay(date=day.date, stops=[e[0] for e in entries], segments=segments,
                    warnings=warnings, total_km=total_km)


async def compute_route_plan(prof: TravelProfile, session_factory=None) -> dict:
    """路线计算主入口（全确定性）。返回 {"days": [RouteDay 字典], "notice": str|None}，
    绝不抛出（外部通道失败逐级降级），调用方将 days 归一化为 RouteDay 写黑板。
    session_factory：高德会话工厂，None=生产路径 amap_session()；测试注入桩。"""
    if not McpConfig.AMAP_API_KEY:
        return {"days": [], "notice": "未配置高德 Key，路线规划跳过"}
    if prof.draft is None or not prof.draft.days:
        return {"days": [], "notice": "黑板无草稿，路线规划跳过"}
    city = prof.basic_info.destination or ""
    if not city:
        return {"days": [], "notice": "目的地缺失，路线规划跳过"}
    if session_factory is None:
        from .mcp_client import amap_session
        session_factory = amap_session
    try:
        session = session_factory()
    except Exception as exc:  # noqa: BLE001 — 通道不可用整体跳过
        logger.warning("高德会话创建失败（%s: %s），路线规划跳过", type(exc).__name__, exc)
        return {"days": [], "notice": "高德通道不可用，路线规划跳过"}
    budget = _Budget()
    cache: dict[str, tuple[float, float] | None] = {}

    hotel_pos = None
    selected = next((h for h in prof.hotels if h.selected), None)
    if selected is not None and session is not None:
        hotel_geo = await _geocode(session, budget, cache, selected.name, city)
        hotel_pos = hotel_geo[0] if hotel_geo else None

    dishes = ([fn.name for fn in (prof.food_notes or []) if fn.name][:4]
              or list(kb_for_city(city).get("foods", []))[:4] or ["餐厅"])
    restrictions = [r for r in (prof.detail_info.food_restrictions or []) if r]

    days = []
    for i, d in enumerate(prof.draft.days):
        days.append(await _day_route(session, budget, cache, d, i, city, dishes,
                                     restrictions, hotel_pos))
    notice = "路线计算超出时间预算，部分段间为离线估算（参考值）" if budget.expired() else None
    return {"days": [d.model_dump(mode="json") for d in days], "notice": notice}


_REVIEW_PROMPT = """你是旅行路线审核员。下面是「{city}」行程的每日路线计算结果（站点与段间交通）。
饮食禁忌：{restrictions}
请逐日检查并输出 JSON（不要输出任何其他文字）：
{{"days": [{{"index": 1, "summary": "一句话点评（≤40字，如路线顺/回头路/时间紧张）", "warning": "无或一句话问题提示（≤50字，如两站相距过远、餐厅与禁忌冲突、站点间跨城）"}}]}}
index 从 1 开始对应天数；没有问题的天 warning 写"无"。

路线数据：
{routes}"""


async def review_route_plan(client, plan: dict, prof: TravelProfile) -> dict:
    """LLM 审查（digest.py 同款容错）：每日一句点评 + 问题警告。任何失败返回空 dict
    （路线照常入黑板，只是没有点评）；熔断异常由调用方（collect 工具）兜住透传。"""
    days = plan.get("days") or []
    if not days:
        return {}
    lines = []
    for i, d in enumerate(days):
        stops = " → ".join(str(s.get("name", "")) for s in (d.get("stops") or []))
        segs = "；".join(f"{s.get('mode')}"
                         + (f"{s.get('distance_m', 0)}m" if s.get("distance_m") else "")
                         + (f"/{s.get('duration_min', 0)}min" if s.get("duration_min") else "")
                         for s in (d.get("segments") or []) if s.get("mode"))
        lines.append(f"第{i + 1}天（{d.get('date', '')}）：{stops or '无'}｜段间：{segs or '无'}")
    prompt = _REVIEW_PROMPT.format(city=prof.basic_info.destination or "",
                                   restrictions="、".join(prof.detail_info.food_restrictions or []) or "无",
                                   routes="\n".join(lines))

    async def _call() -> str:
        result = await client.create([UserMessage(content=prompt, source="route_review")])
        return result.content if isinstance(result.content, str) else ""

    try:
        text = await asyncio.wait_for(_call(), timeout=_REVIEW_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — 审查失败静默跳过（不阻塞路线入黑板）
        return {}
    from ..digest import _parse_json_block
    data = _parse_json_block(text)
    return data if isinstance(data, dict) else {}


def merge_review(plan: dict, review: dict) -> list[RouteDay]:
    """审查结果按天合并进路线（确定性、防御式：index 越界/字段缺失一律忽略）。"""
    routes = [RouteDay(**d) for d in (plan.get("days") or [])]
    for it in (review.get("days") or []) if isinstance(review, dict) else []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("index", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(routes)):
            continue
        summary = str(it.get("summary") or "").strip()
        if summary:
            routes[idx].summary = summary[:60]
        warning = str(it.get("warning") or "").strip()
        if warning and warning not in ("无", "无。", "暂无"):
            routes[idx].warnings.append(warning[:80])
    return routes
