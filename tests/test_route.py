"""route 通道单元测试：URI 编码、饭点策略、段间解析、降级链、deadline 护栏（全无网，桩会话）。"""

import asyncio
import json

import pytest

from tripmate.config import McpConfig, RouteConfig
from tripmate.models import (BasicInfo, DetailInfo, Draft, DraftDay, FoodNote,
                             GuideDigestItem, HotelCandidate, SpotNote, TicketCandidate,
                             TravelProfile)
from tripmate.tools.route import (_nav_url, _parse_direction, _pick_mode,
                                  _pick_restaurant, compute_route_plan, merge_review,
                                  review_route_plan)


class StubSession:
    """可编程桩会话：接口与 McpSession.call 一致，按关键词子串返回预设响应/抛错。"""

    def __init__(self, responses=None, fail_keywords=()):
        self.calls = []
        self.responses = responses or {}
        self.fail_keywords = fail_keywords

    async def call(self, keywords, args, what):
        key = "|".join(keywords)
        self.calls.append((key, dict(args)))
        if any(f in key for f in self.fail_keywords):
            raise RuntimeError(f"stub fail: {key}")
        for k, v in self.responses.items():
            if k in key:
                return v
        return {}


def _prof(spots=("钟楼", "陕西历史博物馆", "大雁塔"), restrictions=()):
    return TravelProfile(
        basic_info=BasicInfo(destination="西安", days=1),
        detail_info=DetailInfo(food_restrictions=list(restrictions)),
        food_notes=[FoodNote(name="肉夹馍"), FoodNote(name="羊肉泡馍")],
        hotels=[HotelCandidate(name="全季酒店（西安钟楼店）", price_per_night=350, distance_km=1,
                               rating=4.5, link="", selected=True)],
        draft=Draft(days=[DraftDay(date="2026-10-01", spots=list(spots))]),
    )


# ---- 纯函数 ----

def test_nav_url_encodes_name_and_omits_from():
    url = _nav_url(108.94, 34.26, "老孙家泡馍&分店", "公交")
    assert url.startswith("https://uri.amap.com/navigation?to=108.94,34.26,")
    assert "from=" not in url                       # 省略 from = GPS 当前位置出发
    assert "%26" in url                             # 名称内 & 被编码，不截断参数
    assert "mode=bus" in url
    assert "老孙家" not in url                       # 中文已整体编码


def test_pick_mode_thresholds():
    assert _pick_mode(800)[0] == "步行"
    assert _pick_mode(5000)[0] == "公交"
    mode, cross = _pick_mode(30000)
    assert mode == "驾车" and cross is True


def test_pick_restaurant_filters_restrictions():
    pois = [{"name": "王家猪肉铺", "id": "1"}, {"name": "老米家泡馍", "id": "2", "address": "东大街"}]
    pick = _pick_restaurant(pois, ["猪肉"])
    assert pick == ("老米家泡馍", "东大街", "2")
    assert _pick_restaurant(pois, ["泡馍", "猪肉"]) is None


def test_parse_direction_shapes():
    walk = _parse_direction({"route": {"paths": [{"distance": 5080, "duration": 4064}]}}, "步行")
    assert walk.mode == "步行" and walk.distance_m == 5080 and walk.duration_min == 68
    # 公交（实测无 route 包装，字段在顶层，数值为字符串）
    transit = _parse_direction({"distance": "6157",
                                "transits": [{"duration": "3500"}, {"duration": "2949"}]}, "公交")
    assert transit.mode == "公交" and transit.distance_m == 6157 and transit.duration_min == 49
    assert _parse_direction({"foo": 1}, "公交") is None


# ---- compute_route_plan：短路 / 降级 / deadline / 正常路径 ----

def test_compute_short_circuits(monkeypatch):
    async def main():
        monkeypatch.setattr(McpConfig, "AMAP_API_KEY", "")
        r = await compute_route_plan(_prof(), session_factory=lambda: (_ for _ in ()).throw(AssertionError("不该建会话")))
        assert r["days"] == [] and "未配置高德 Key" in r["notice"]
        monkeypatch.setattr(McpConfig, "AMAP_API_KEY", "k")
        r2 = await compute_route_plan(TravelProfile(basic_info=BasicInfo(destination="西安")),
                                      session_factory=StubSession())
        assert r2["days"] == [] and "无草稿" in r2["notice"]
    asyncio.run(main())


def test_compute_full_with_stub():
    """桩会话正常路径：坐标/段间/饭点餐厅（detail 补坐标）全链路产出真实结构。"""
    stub = StubSession(responses={
        "text|search": {"pois": [{"name": "P", "location": "108.90,34.20", "address": "测试路1号"}]},
        "around": {"pois": [{"name": "好再来餐厅", "address": "某路1号", "id": "B001"}]},
        "detail": {"location": "108.91,34.21"},
        "direction|walking": {"route": {"paths": [{"distance": 900, "duration": 700}]}},
        "direction|transit": {"distance": "6000", "transits": [{"duration": "2400"}]},
        "direction|driving": {"route": {"paths": [{"distance": 30000, "duration": 2700}]}},
    })

    async def main():
        return await compute_route_plan(_prof(), session_factory=lambda: stub)

    r = asyncio.run(main())
    assert r["notice"] is None
    day = r["days"][0]
    kinds = [s["kind"] for s in day["stops"]]
    assert kinds == ["spot", "spot", "meal", "spot", "meal"]      # 3 景点 + 中点午餐 + 末尾晚餐
    lunch = day["stops"][2]
    assert lunch["name"] == "好再来餐厅" and lunch["meal"] == "午餐"
    assert day["stops"][0]["address"] == "测试路1号"   # 景点行地址小字（e2e 视觉验收缺口回归）
    assert lunch["lon"] == 108.91 and lunch["nav_url"].startswith("https://uri.amap.com/")
    assert day["stops"][4]["meal"] == "晚餐"
    assert all(seg["mode"] and not seg["reference_only"] for seg in day["segments"])
    assert len(day["segments"]) == len(day["stops"]) - 1
    assert day["total_km"] > 0


def test_compute_degrades_when_mcp_down():
    """全通道失败：坐标未获取/段间置空/饭点自由安排，绝不抛出、不超预算。"""
    stub = StubSession(fail_keywords=("direction", "around", "detail", "text"))

    async def main():
        return await compute_route_plan(_prof(), session_factory=lambda: stub)

    r = asyncio.run(main())
    day = r["days"][0]
    assert all(s["note"] == "坐标未获取" for s in day["stops"] if s["kind"] == "spot")
    assert all(s["reference_only"] for s in day["stops"] if s["kind"] == "meal")
    assert all(not seg["mode"] for seg in day["segments"])        # 端点缺坐标 → 空段占位
    assert r["notice"] is None                                    # 快速失败未耗尽预算


def test_compute_deadline_guard(monkeypatch):
    """总预算耗尽：不再发起任何 MCP 调用，直接离线完成并标注。"""
    monkeypatch.setattr(RouteConfig, "BUDGET_S", 0.0)
    stub = StubSession()

    async def main():
        return await compute_route_plan(_prof(), session_factory=lambda: stub)

    r = asyncio.run(main())
    assert stub.calls == []                                       # 预算先于一切外部调用检查
    assert "预算" in r["notice"]


# ---- LLM 审查合并 ----

class _StubClient:
    def __init__(self, content="", exc=None):
        self._content, self._exc = content, exc

    async def create(self, messages, **kw):
        if self._exc:
            raise self._exc
        class _R:
            content = self._content
        return _R()


def test_review_route_plan_tolerant():
    plan = {"days": [{"date": "2026-10-01", "stops": [{"name": "钟楼"}], "segments": []}]}
    good = asyncio.run(review_route_plan(
        _StubClient(content='前言 {"days": [{"index": 1, "summary": "顺路", "warning": "无"}]} 后记'),
        plan, _prof()))
    assert good["days"][0]["summary"] == "顺路"
    bad = asyncio.run(review_route_plan(_StubClient(exc=RuntimeError("llm down")), plan, _prof()))
    assert bad == {}                                              # 失败静默，路线照常入黑板


def test_merge_review_defensive():
    plan = {"days": [{"date": "d1", "stops": [], "segments": [], "warnings": []},
                     {"date": "d2", "stops": [], "segments": [], "warnings": []}]}
    routes = merge_review(plan, {"days": [
        {"index": 1, "summary": "顺路", "warning": "无"},
        {"index": 2, "summary": "", "warning": "下午两站较远"},
        {"index": 9, "summary": "越界忽略"},
        {"index": "x", "summary": "非法忽略"},
    ]})
    assert routes[0].summary == "顺路" and routes[0].warnings == []
    assert routes[1].summary == "" and routes[1].warnings == ["下午两站较远"]


# ---- finalize 护栏与定稿保险（TeamRunner 集成，compute 打桩，无网）----

def test_finalize_guardrail_and_insurance_fill_routes(monkeypatch):
    """流内路线步骤未产出时：护栏（阶段收尾）与 _deliver_final 保险（渲染前）都补算写分区。"""
    from tripmate.blackboard import Blackboard
    from tripmate.status import StatusBus
    from tripmate.team import JobBoard, TeamContext, TeamRunner, TeamState
    import tripmate.team as team_mod

    async def fake_compute(prof, session_factory=None):
        return {"days": [{"date": "2026-10-01",
                          "stops": [{"kind": "spot", "name": "宽窄巷子"}],
                          "segments": [], "warnings": [], "total_km": 0}],
                "notice": None}

    async def fake_images(spots, city=""):
        return {"items": [], "notice": "", "mode": "mock"}

    async def fake_covers(city):
        return []

    monkeypatch.setattr(team_mod, "compute_route_plan", fake_compute)
    monkeypatch.setattr(team_mod, "search_images", fake_images)
    monkeypatch.setattr(team_mod, "search_city_covers", fake_covers)

    def _bb():
        # 预置已收集分区：_ensure_sections 的公共护栏块（攻略/车票/酒店/天气）不分阶段执行，
        # 空分区会触发真实 MCP/LLM 调用——真实 finalize 时这些分区早已就绪，此处与之对齐
        bb = Blackboard()
        bb.profile.basic_info = BasicInfo(origin="上海", destination="成都", days=1)
        bb.profile.guide_digest = [GuideDigestItem(
            source_name="测试来源", source_url="https://example.com", fetched_at="2026-09-06",
            spots=["宽窄巷子"], foods=[], routes=[], warnings=[])]
        bb.profile.tickets = [TicketCandidate(train_no="G-TEST", depart_time="08:00",
                                              arrive_time="12:00", duration_min=240, price=500,
                                              link="", selected=True)]
        bb.profile.hotels = [HotelCandidate(name="测试酒店", price_per_night=300, distance_km=1,
                                            rating=4.5, link="", selected=True)]
        bb.profile.weather = {"days": []}
        bb.profile.spot_notes = [SpotNote(name="宽窄巷子", intro="测试")]
        bb.profile.draft = Draft(days=[DraftDay(date="2026-10-01", spots=["宽窄巷子"])])
        return bb

    async def main():
        # 护栏：阶段收尾时 routes 缺失 → 确定性补算
        bb = _bb()
        runner = TeamRunner(bb, StatusBus())
        runner._digest_done = runner._cover_done = True   # 跳过 LLM 提炼/封面检索（幂等标志）
        ctx = TeamContext(bb=bb, bus=StatusBus(), state=TeamState(phase="finalize"),
                          jobs=JobBoard(), runner=runner, run_id="guardrail01")
        await runner._ensure_sections(ctx, "finalize", [])
        assert bb.profile.routes and bb.profile.routes[0].stops[0].name == "宽窄巷子"

        # 保险：routes 缺失时 deliver_final 渲染前补算（与护栏双保险，均幂等）
        bb2 = _bb()
        runner2 = TeamRunner(bb2, StatusBus())
        runner2._digest_done = runner2._cover_done = True
        ctx2 = TeamContext(bb=bb2, bus=StatusBus(), state=TeamState(phase="finalize"),
                           jobs=JobBoard(), runner=runner2, run_id="insurance01")
        out = await team_mod._deliver_final(ctx2)
        assert bb2.profile.routes and bb2.profile.final is not None
        assert json.loads(out)["route_days"] == 1

    asyncio.run(main())
