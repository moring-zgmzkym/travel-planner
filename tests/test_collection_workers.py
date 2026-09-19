"""收集侧多 worker 回归（2026-09-19）：structure_guide_rows 结构化下沉 / covers+foods_img
通道收割与单通道降级 / FIELD_IMPACT 新映射 / S4 复用路径（food_restrictions 变更只重提炼）。

全程离线：LLM 客户端、search_guides、search_city_covers、build_digest_notes、
run_channel_subagent 全部 monkeypatch——不耗真实额度、不受 .env 影响。
"""

import asyncio
import json
import types

from tripmate.blackboard import Blackboard
from tripmate.config import JobConfig
from tripmate.models import FoodNote, GuideDigestItem, SpotNote
from tripmate.planning import analyze_impact
from tripmate.status import StatusBus
from tripmate.team import JobBoard, TeamContext, TeamState, make_researcher_tools


# ---- 桩件 ----

def _real_row(name="小红书检索"):
    """真实通道形态的攻略原始行（含 raw_answer/标题，结构化四元缺省）。"""
    return {"source_name": name, "source_url": "https://example.com/x", "fetched_at": "2026-09-19 10:00",
            "raw_answer": "汉中石门栈道是陕南代表性景点，可以看古道石刻。附近热面皮和菜豆腐很有名，"
                          "下午顺路去武侯祠，旺季石门栈道需要提前预约。",
            "raw_titles": ["汉中石门栈道攻略", "汉中美食推荐"], "raw_urls": ["https://example.com/y"],
            "reference_only": False}


def _rows_json(payload: str):
    """按 structure_guide_rows 的解析路径构造伪 LLM 客户端（记录调用次数）。"""
    class _FakeClient:
        def __init__(self):
            self.calls = 0

        async def create(self, messages, **kwargs):
            self.calls += 1
            return types.SimpleNamespace(content=payload)
    return _FakeClient()


def _ctx(bb: Blackboard, jb: JobBoard, reuse: dict | None = None) -> TeamContext:
    return TeamContext(bb=bb, bus=StatusBus(), state=TeamState(), jobs=jb,
                       runner=None, run_id="t", reuse=reuse or {})


async def _hang_job():
    await asyncio.sleep(30)


def _fake_subagent(calls: list):
    """直通版 run_channel_subagent：跳过 LLM，直接执行查询闭包（保留 notify 时序）。"""
    async def fake(channel, instruction, query, model_client=None, notify=None):
        calls.append(channel)
        if notify is not None:
            await notify("running")
        out = await query()
        if notify is not None:
            await notify("done")
        return out
    return fake


# ---- structure_guide_rows：结构化下沉（digest.py）----

def test_structure_rows_fills_structured_fields(monkeypatch):
    from tripmate.digest import structure_guide_rows
    payload = ('{"rows": [{"spots": ["石门栈道", "武侯祠"], "foods": ["热面皮"], '
               '"routes": ["武侯祠→石门栈道"], "warnings": ["旺季提前预约"]}]}')
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: _rows_json(payload))
    rows = [_real_row()]

    async def main():
        return await structure_guide_rows(rows, "汉中")
    out = asyncio.run(main())

    assert out[0]["spots"] == ["石门栈道", "武侯祠"]
    assert out[0]["foods"] == ["热面皮"]
    assert out[0]["routes"] == ["武侯祠→石门栈道"]
    assert out[0]["warnings"] == ["旺季提前预约"]
    # raw 字段保留（test_guide_digest 契约：真实通道内容不得在整理中丢失）
    assert out[0]["raw_answer"] == rows[0]["raw_answer"]
    assert out[0]["reference_only"] is False


def test_structure_rows_keeps_abbreviation_filters_fabricated(monkeypatch):
    """软校验：原文含"石门栈道"时简称"栈道"经 2-gram 命中保留；零重叠编造项被过滤。"""
    from tripmate.digest import structure_guide_rows
    payload = ('{"rows": [{"spots": ["栈道景区", "兵马俑"], "foods": [], '
               '"routes": [], "warnings": []}]}')
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: _rows_json(payload))

    async def main():
        return await structure_guide_rows([_real_row()], "汉中")
    out = asyncio.run(main())

    assert out[0]["spots"] == ["栈道景区"]


def test_structure_rows_all_filtered_falls_back(monkeypatch):
    """全部条目被过滤 → 视为未产出，原样回退原始行。"""
    from tripmate.digest import structure_guide_rows
    payload = '{"rows": [{"spots": ["兵马俑"], "foods": [], "routes": [], "warnings": []}]}'
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: _rows_json(payload))
    rows = [_real_row()]

    async def main():
        return await structure_guide_rows(rows, "汉中")
    out = asyncio.run(main())

    assert "spots" not in out[0]


def test_structure_rows_llm_failure_falls_back(monkeypatch):
    """LLM 超时/异常：原样返回 rows，绝不上抛（否则被 subagent 降级链放大成多次查询）。"""
    from tripmate.digest import structure_guide_rows
    monkeypatch.setattr("tripmate.digest._STRUCT_TIMEOUT_S", 0.05)

    class _SlowClient:
        async def create(self, messages, **kwargs):
            await asyncio.sleep(1)
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: _SlowClient())
    rows = [_real_row()]

    async def main():
        return await structure_guide_rows(rows, "汉中")
    out = asyncio.run(main())

    assert out == rows


def test_structure_rows_malformed_json_falls_back(monkeypatch):
    from tripmate.digest import structure_guide_rows
    monkeypatch.setattr("tripmate.digest.get_model_client",
                        lambda: _rows_json("抱歉，我无法输出 JSON。"))
    rows = [_real_row()]

    async def main():
        return await structure_guide_rows(rows, "汉中")
    out = asyncio.run(main())

    assert out == rows


def test_structure_rows_bare_array_output(monkeypatch):
    """模型输出裸 JSON 数组（不带 rows 键/带 markdown 围栏）：容忍解析。"""
    from tripmate.digest import structure_guide_rows
    payload = '```json\n[{"spots": ["石门栈道"], "foods": ["热面皮"], "routes": [], "warnings": []}]\n```'
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: _rows_json(payload))

    async def main():
        return await structure_guide_rows([_real_row()], "汉中")
    out = asyncio.run(main())

    assert out[0]["spots"] == ["石门栈道"] and out[0]["foods"] == ["热面皮"]


def test_structure_rows_mock_passthrough_without_llm(monkeypatch):
    """mock 行（无原文）直接跳过，不消耗 LLM 调用。"""
    from tripmate.digest import structure_guide_rows
    client = _rows_json('{"rows": []}')
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: client)
    rows = [{"source_name": "模拟", "source_url": "", "fetched_at": "t",
             "spots": ["石门栈道"], "foods": ["热面皮"], "reference_only": True}]

    async def main():
        return await structure_guide_rows(rows, "汉中")
    out = asyncio.run(main())

    assert client.calls == 0 and out == rows


# ---- finish_guide_search：单通道失败隔离 ----

def test_finish_degrades_covers_channel_only(monkeypatch):
    """covers 通道挂死：攻略分区照常写入，covers 收割降级为 0，整体仍 status=written。"""
    monkeypatch.setattr(JobConfig, "TIMEOUT_S", 0.2)
    bb = Blackboard()
    jb = JobBoard()

    async def _ok_guides():
        return {"mode": "real", "digest": [_real_row()]}

    async def main():
        jb.submit("guides", _ok_guides())
        jb.submit("covers", _hang_job())
        ctx = _ctx(bb, jb)
        finish_tool = make_researcher_tools(ctx)[1]
        data = json.loads(await finish_tool())
        return data
    data = asyncio.run(main())

    assert data["status"] == "written" and data["sources"] == 1
    assert data["covers"] == 0
    assert len(bb.profile.guide_digest) == 1
    assert bb.profile.cover_images == []


def test_finish_skips_unsubmitted_workers(monkeypatch):
    """foods_img/covers 未提交（如自愈路径只跑了 guides）：判空跳过，不抛 RuntimeError。"""
    bb = Blackboard()
    jb = JobBoard()

    async def _ok_guides():
        return {"mode": "mock", "digest": [{"source_name": "模拟", "source_url": "",
                                            "fetched_at": "t", "spots": ["石门栈道"],
                                            "reference_only": True}]}

    async def main():
        jb.submit("guides", _ok_guides())
        ctx = _ctx(bb, jb)
        finish_tool = make_researcher_tools(ctx)[1]
        return json.loads(await finish_tool())
    data = asyncio.run(main())

    assert data["status"] == "written" and data["covers"] == 0 and data["food_notes"] == 0
    assert len(bb.profile.guide_digest) == 1


# ---- 多 worker 全链路（离线集成）----

def test_researcher_three_workers_end_to_end(monkeypatch):
    """start 提交 guides+covers、链式提交 foods_img；finish 逐通道收割写三分区。"""
    import tripmate.team as team_mod
    payload = ('{"rows": [{"spots": ["石门栈道"], "foods": ["热面皮"], '
               '"routes": [], "warnings": []}]}')
    monkeypatch.setattr("tripmate.digest.get_model_client", lambda: _rows_json(payload))
    monkeypatch.setattr(team_mod, "search_guides", _mk_guides())
    monkeypatch.setattr(team_mod, "search_city_covers", _mk_covers())
    monkeypatch.setattr(team_mod, "build_digest_notes", _mk_notes())
    calls: list[str] = []
    monkeypatch.setattr(team_mod, "run_channel_subagent", _fake_subagent(calls))

    bb = Blackboard()
    jb = JobBoard()

    async def main():
        ctx = _ctx(bb, jb)
        tools = make_researcher_tools(ctx)
        await tools[0]()            # start_guide_search：提交 guides+covers
        assert jb.has("guides") and jb.has("covers")
        await asyncio.sleep(0.05)   # 链式提交的 foods_img 任务启动
        assert jb.has("foods_img"), "结构化产出美食清单后应链式提交美食图文任务"
        return json.loads(await tools[1]())     # finish_guide_search
    data = asyncio.run(main())

    assert sorted(calls) == ["covers", "foods_img", "guides"]
    assert data["status"] == "written" and data["covers"] == 1 and data["food_notes"] == 1
    assert bb.profile.guide_digest[0].spots == ["石门栈道"]
    assert bb.profile.cover_images == ["/tmp/fake_cover.jpg"]
    assert bb.profile.spot_notes[0].name == "石门栈道"
    assert bb.profile.food_notes[0].name == "热面皮"


def _mk_guides():
    async def _search_guides(dest, month, style_hint=""):
        return {"mode": "real", "digest": [_real_row()]}
    return _search_guides


def _mk_covers():
    async def _covers(city):
        return ["/tmp/fake_cover.jpg"]
    return _covers


def _mk_notes():
    async def _notes(city, texts):
        assert texts, "美食图文语料应来自攻略原文"
        return {"spots": [SpotNote(name="石门栈道", intro="古道石刻")],
                "foods": [FoodNote(name="热面皮", intro="香辣")]}
    return _notes


# ---- S4 复用路径：food_restrictions 变更 → guides 复用、foods_img 从黑板语料重跑 ----

def test_reuse_path_resubmits_foods_from_blackboard(monkeypatch):
    import tripmate.team as team_mod
    monkeypatch.setattr(team_mod, "build_digest_notes", _mk_notes())
    calls: list[str] = []
    monkeypatch.setattr(team_mod, "run_channel_subagent", _fake_subagent(calls))

    bb = Blackboard()
    bb.profile.basic_info.destination = "汉中"
    jb = JobBoard()

    async def main():
        await bb.write("guide_digest", [GuideDigestItem(source_name="小红书检索", source_url="",
                                                        fetched_at="t", raw_answer="汉中石门栈道攻略原文……")],
                       "researcher", "上一轮攻略")
        ctx = _ctx(bb, jb, reuse={"guides": True, "covers": True, "foods_img": False})
        tools = make_researcher_tools(ctx)
        data = json.loads(await tools[0]())   # start：复用分支
        assert data["status"] == "reused"
        assert not jb.has("guides") and not jb.has("covers")
        assert jb.has("foods_img"), "food_restrictions 变更应从黑板攻略语料重新提交美食图文"
        await asyncio.sleep(0.05)
        return json.loads(await tools[1]())   # finish：复用分支收割 foods
    data = asyncio.run(main())

    assert data["status"] == "reused" and data["food_notes"] == 1
    assert bb.profile.spot_notes[0].name == "石门栈道"
    assert bb.profile.food_notes[0].name == "热面皮"


# ---- FIELD_IMPACT 新映射 ----

def test_field_impact_new_channels():
    affected = analyze_impact(["destination"])
    assert {"guides", "covers", "foods_img"} <= affected
    assert analyze_impact(["food_restrictions"]) == {"foods_img", "itinerary"}
    assert "covers" not in analyze_impact(["budget"])   # 只改预算不重搜封面
