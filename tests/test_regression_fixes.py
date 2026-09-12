"""2026-09-12 回归修复守护测试：路线饭点无坐标降级、酒店 dict 容错与回退估算标注、
攻略单路畸形容错、兜底草稿日期、大巴票价人数折算、MCP 握手并发防护（全无网，桩会话/桩函数）。"""

import asyncio
from datetime import date, timedelta

import pytest

from tripmate.config import McpConfig, SearchConfig
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, FoodNote,
                             HotelCandidate, TravelProfile)
from tripmate.tools.hotels import _normalize_hotels, query_hotels
from tripmate.tools.mcp_client import PersistentMcpSession
from tripmate.tools.route import compute_route_plan
from tripmate.tools.search import search_guides
from tripmate.tools.tickets import BUS_PRICE_PER_KM, query_tickets
from tripmate.mocks.data import mock_transport_estimate_km


# ---- 修复 2：饭点餐厅无坐标 → 仅相邻段空段占位，不拖垮全天路线 ----

def test_route_meal_without_coords_degrades_per_segment(monkeypatch):
    """此前餐厅坐标缺失时插入 (None, None) 元组：绕过空段判断 → _haversine(None) TypeError
    → 整次路线报废。修复后应与"景点坐标缺失"同路径：相邻段置空，其余段正常。"""

    class SpotOnlyGeoStub:
        """仅三个景点名返回坐标；餐厅（around→detail→按名补查）一律无坐标。"""

        def __init__(self):
            self.calls = []

        async def call(self, keywords, args, what):
            key = "|".join(keywords)
            self.calls.append((key, dict(args)))
            if key == "detail":
                return {}
            if key == "around":
                return {"pois": [{"name": "好再来餐厅", "address": "某路1号", "id": "B001"}]}
            if key == "text|search":
                kw = str(args.get("keywords", ""))
                if kw in ("钟楼", "陕西历史博物馆", "大雁塔"):
                    return {"pois": [{"name": kw, "location": "108.90,34.20", "address": "某路2号"}]}
                return {}  # 餐厅按名补查：无坐标
            if key.startswith("direction"):
                return {"route": {"paths": [{"distance": 900, "duration": 700}]}}
            return {}

    async def main():
        monkeypatch.setattr(McpConfig, "AMAP_API_KEY", "k")
        prof = TravelProfile(
            basic_info=BasicInfo(destination="西安", days=1),
            detail_info=DetailInfo(),
            food_notes=[FoodNote(name="肉夹馍")],
            hotels=[HotelCandidate(name="全季酒店（西安钟楼店）", price_per_night=350, distance_km=1,
                                   rating=4.5, link="", selected=True)],
            draft=Draft(days=[DraftDay(date="2026-10-01",
                                       spots=["钟楼", "陕西历史博物馆", "大雁塔"])]),
        )
        r = await compute_route_plan(prof, session_factory=lambda: SpotOnlyGeoStub())
        assert r["days"], "路线整体不应失败"
        day = r["days"][0]
        stops, segs = day["stops"], day["segments"]
        assert len(segs) == len(stops) - 1, "空段占位须保持 stops/segments 对齐"
        meals = [s for s in stops if s["kind"] == "meal" and s["name"] != "晚餐：自由安排"]
        assert meals and all("坐标未获取" in (s["note"] or "") for s in meals)
        empty = [s for s in segs if s["distance_m"] == 0]
        real = [s for s in segs if s["distance_m"] > 0]
        assert real, "有坐标的段间仍应正常计算"
        assert len(empty) >= 2, "餐厅相邻段应为空段占位而非全线报废"

    asyncio.run(main())


# ---- 修复 3：酒店 MCP dict 形态响应不再 TypeError 绕过降级链 ----

def test_normalize_hotels_dict_shape_tolerant():
    wrapped = {"hotels": {"name": "测试酒店", "price": "¥300"}}
    out = _normalize_hotels(wrapped, nights=1)
    assert out and out[0]["name"] == "测试酒店" and out[0]["price_per_night"] == 300.0
    assert _normalize_hotels({"foo": "bar"}, nights=1) == []  # 无法提取 → 空列表（上层走 mock 降级）
    assert _normalize_hotels("garbage", nights=1) == []


# ---- 修复 4：攻略单路畸形响应只降级该路，不丢其余已成功结果 ----

def test_search_guides_tolerates_malformed_route(monkeypatch):
    async def fake_tavily(client, query, search_depth="basic", max_results=5):
        if "xiaohongshu" in query:
            return {"answer": "a1", "results": [{"url": "https://xhs.cn/1", "title": "t1"}]}
        if "mafengwo" in query:
            return {"answer": "a2", "results": None}          # 单路畸形：results=null
        if "美食攻略" in query:
            return "not-a-dict"                                # 单路畸形：非 dict
        if "避坑" in query:
            return {"answer": "a4", "results": [{"title": "缺 url 键"}]}
        return {"answer": "ok", "results": [{"url": f"https://ok.cn/{query[:4]}", "title": "t"}]}

    async def main():
        monkeypatch.setattr(SearchConfig, "TAVILY_API_KEY", "k")
        monkeypatch.setattr("tripmate.tools.search._tavily", fake_tavily)
        r = await search_guides("西安")
        assert r["mode"] == "real"
        names = [d["source_name"] for d in r["digest"]]
        assert len(r["digest"]) == 4, f"3 路畸形应被跳过、4 路正常保留，实际 {names}"
        assert not any("马蜂窝" in n or "美食" in n or "避坑" in n for n in names)

    asyncio.run(main())


# ---- 修复 5：兜底草稿日期从今天起算，不再硬编码 2026-10-01 ----

def test_fallback_draft_dates_from_today():
    from tripmate.team import _fallback_draft
    prof = TravelProfile(basic_info=BasicInfo(destination="西安", days=2))
    draft = _fallback_draft(prof)
    today = date.today()
    assert draft.days[0].date == today.isoformat()
    assert draft.days[1].date == (today + timedelta(days=1)).isoformat()


# ---- 修复 6：长途大巴估算价按全团人数折算 ----

def test_bus_ticket_scales_with_party():
    km = mock_transport_estimate_km("上海", "南京")

    async def main():
        r1 = await query_tickets("上海", "南京", ["2026-10-01"], "长途大巴")
        r4 = await query_tickets("上海", "南京", ["2026-10-01"], "长途大巴", party=4)
        assert r1["candidates"][0]["price"] == pytest.approx(round(km * 2 * BUS_PRICE_PER_KM, 1))
        assert r4["candidates"][0]["price"] == pytest.approx(round(km * 2 * BUS_PRICE_PER_KM * 4, 1))
        assert "4" in r4["notice"] or "4 人" in r4["notice"], "降价口径须注明按人数估算"
        # 自驾按车计费，不随人数折算
        r_car = await query_tickets("上海", "南京", ["2026-10-01"], "自驾", party=4)
        assert r_car["candidates"][0]["price"] == pytest.approx(round(km * 2 * 0.8, 1))

    asyncio.run(main())


# ---- 修复 7：价格回退全量时补 distance_estimated 标注，假 1km 不再冒充真实距离 ----

def test_hotels_fallback_marks_distance_estimated(monkeypatch):
    """钉住 mock 路径（.env 可能配置了真实 MCP）：价格上限 1 分钱 → 全部候选被过滤 → 回退全量。"""
    monkeypatch.setattr(McpConfig, "MCP_HOTEL_URL", "")
    monkeypatch.setattr(McpConfig, "AMAP_API_KEY", "")
    monkeypatch.setattr("tripmate.tools.hotels.ALLOW_MOCK_FALLBACK", True)

    async def main():
        r = await query_hotels("西安", None, [0, 0.01])
        assert r["candidates"], "回退后应有候选"
        assert all(c.get("distance_estimated") for c in r["candidates"]), "回退候选的占位距离须标注为估算"
        assert "放宽" in (r["notice"] or "")

    asyncio.run(main())


# ---- 修复 9：PersistentMcpSession 并发握手防护——两次并发只开一次握手 ----

def test_mcp_ensure_open_concurrent_single_handshake():
    async def main():
        s = PersistentMcpSession("stdio", command="npx -y stub-mcp", name="stub")
        opens = []

        async def fake_supervise():
            opens.append(1)
            await asyncio.sleep(0.05)          # 模拟 npx 冷启动窗口
            s._session = object()
            s._tools = []
            s._ready.set()
            await s._stop.wait()

        s._supervise_open = fake_supervise      # 实例属性遮蔽方法，免拉真子进程
        await asyncio.gather(s._ensure_open(), s._ensure_open())
        assert len(opens) == 1, f"并发握手应只开一次，实际 {len(opens)} 次"
        s._stop.set()                            # 收尾：放行 supervisor 退出，防挂起任务告警
        await asyncio.sleep(0.01)

    asyncio.run(main())
