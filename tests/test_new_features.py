"""需求 1-7 新增能力单测：攻略查询扩容 / 图源优先级 / 会话注册表 / 酒店 enrich 结构。"""

from tripmate.blackboard import Blackboard
from tripmate.models import BasicInfo
from tripmate.tools.search import _host_rank, _guide_queries
from tripmate.gateway.app import _session_title


def test_guide_queries_eight_routes_with_style():
    """攻略 8 路扩容：站点 3 路 + 主题 5 路；style 拼入景点专题；新增出片机位专题。"""
    qs = _guide_queries("成都", "十月", "休闲 美食")
    names = [n for _, n in qs]
    assert len(qs) == 8
    assert names[:3] == ["小红书检索", "马蜂窝检索", "全网检索"]
    assert "美食专题" in names and "避坑专题" in names and "路线专题" in names and "景点专题" in names
    assert "机位专题" in names
    spot_q = {n: q for q, n in qs}["景点专题"]
    assert "成都" in spot_q and "休闲 美食" in spot_q
    photo_q = {n: q for q, n in qs}["机位专题"]
    assert "成都" in photo_q and "机位" in photo_q


def test_guide_queries_without_optional_hints():
    """月份/风格缺省时查询仍完整构造（占位符不残留 None）。"""
    qs = _guide_queries("汉中", "", "")
    assert len(qs) == 8
    for q, _ in qs:
        assert "None" not in q and "  " not in q.strip()


def test_host_rank_prefers_authority():
    """权威媒体/官方图源排在普通图源前（稳定排序）。"""
    urls = ["https://img.example.com/a.jpg", "https://img3.chinadaily.com.cn/a.jpeg",
            "https://cdn.foo.net/b.jpg", "http://img.cnwest.com/c.jpg"]
    ranked = sorted(urls, key=_host_rank)
    assert "chinadaily.com" in ranked[0] or "cnwest.com" in ranked[0]
    assert _host_rank("https://img3.chinadaily.com.cn/a.jpeg") == 0
    assert _host_rank("https://cdn.foo.net/b.jpg") == 1


def test_session_title_derives_from_profile():
    """会话标题按黑板状态派生：目的地 + 生命周期阶段（需求 2）。"""
    from tripmate.session import Session
    from tripmate.team import TeamRunner

    s = Session.__new__(Session)
    s.bb = Blackboard()
    s.bb.profile.basic_info = BasicInfo(destination="汉中", days=3)
    s.runner = TeamRunner(s.bb, None)  # active=False, 无 draft/final → 收集需求中
    title = _session_title(s)
    assert title == "汉中 · 收集需求中"

    class _R:  # 模拟运行中
        active = True

    s.runner = _R()
    assert _session_title(s) == "汉中 · 规划中"


def test_days_change_without_new_dates_reexpands_travel_dates():
    """days 变更而未给新日期：按既有出发日重展开逐日序列（防天数/日期口径漂移）。"""
    import asyncio
    from tripmate.chatter import _reexpand_dates_after_days_change
    from tripmate.models import BasicInfo, TravelProfile

    prof = TravelProfile(basic_info=BasicInfo(days=3, travel_dates=["2026-10-01", "2026-10-02", "2026-10-03"]))
    updates = {"days": 4}
    _reexpand_dates_after_days_change(updates, prof)
    assert updates["travel_dates"] == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]


def test_days_change_keeps_explicit_dates_and_ignores_invalid():
    import asyncio
    from tripmate.chatter import _reexpand_dates_after_days_change
    from tripmate.models import BasicInfo, TravelProfile

    prof = TravelProfile(basic_info=BasicInfo(days=3, travel_dates=["2026-10-01", "2026-10-02", "2026-10-03"]))
    # 用户同时给了新日期 → 不覆盖
    updates = {"days": 2, "travel_dates": ["2026-11-01", "2026-11-02"]}
    _reexpand_dates_after_days_change(updates, prof)
    assert updates["travel_dates"] == ["2026-11-01", "2026-11-02"]
    # 画像无既有日期 → 不造日期
    prof2 = TravelProfile(basic_info=BasicInfo(days=3))
    updates2 = {"days": 2}
    _reexpand_dates_after_days_change(updates2, prof2)
    assert "travel_dates" not in updates2


def test_pre_coerce_field_dirty_llm_inputs():
    """LLM 脏输入宽松转型："3天"→3、"6000元"→6000.0、"休闲"→["休闲"]、"300-500"→[300.0,500.0]。"""
    from tripmate.chatter import _pre_coerce_field
    assert _pre_coerce_field("days", "3天") == 3
    assert _pre_coerce_field("party_size", "2大1小") == 2
    assert _pre_coerce_field("budget", "6000元") == 6000.0
    assert _pre_coerce_field("budget_max", "1.2万") == 12000.0
    assert _pre_coerce_field("style", "休闲") == ["休闲"]
    assert _pre_coerce_field("price_range", "300-500") == [300.0, 500.0]
    # 正常值原样通过
    assert _pre_coerce_field("days", 3) == 3
    assert _pre_coerce_field("origin", "上海") == "上海"


def test_check_budget_per_run_baseline_isolation():
    """per-run 基线：多会话各自记账，B 会话重锚不影响 A 的熔断判定（2026-09-05 修复）。"""
    import asyncio
    from autogen_core.models import RequestUsage
    from tripmate import llm
    from tripmate.config import BudgetConfig

    class FakeClient:
        def __init__(self, used):
            self._u = used
        def total_usage(self):
            return self._u

    async def main():
        old = llm._client
        llm._client = FakeClient(RequestUsage(prompt_tokens=100000, completion_tokens=0))
        try:
            baseline_a = llm.snapshot_usage()
            # 进程累计涨到 700000（含 B 会话此前烧掉的 600000）
            llm._client = FakeClient(RequestUsage(prompt_tokens=700000, completion_tokens=0))
            # A 的 per-run 基线是 100000 → 本 run 消耗 600000 = 超限 → 熔断
            try:
                llm.check_budget(baseline_a)
                raised = False
            except llm.TokenBudgetExceeded:
                raised = True
            assert raised
            # 另一会话基线 650000 → 本 run 消耗 50000 → 不熔断（互不干扰）
            llm.check_budget(RequestUsage(prompt_tokens=650000, completion_tokens=0))
        finally:
            llm._client = old

    asyncio.run(main())
