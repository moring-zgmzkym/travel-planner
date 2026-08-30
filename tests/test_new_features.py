"""需求 1-7 新增能力单测：攻略查询扩容 / 图源优先级 / 会话注册表 / 酒店 enrich 结构。"""

from tripmate.blackboard import Blackboard
from tripmate.models import BasicInfo
from tripmate.tools.search import _host_rank, _guide_queries
from tripmate.gateway.app import _session_title


def test_guide_queries_seven_routes_with_style():
    """攻略 7 路扩容：站点 3 路 + 主题 4 路；style 拼入景点专题。"""
    qs = _guide_queries("成都", "十月", "休闲 美食")
    names = [n for _, n in qs]
    assert len(qs) == 7
    assert names[:3] == ["小红书检索", "马蜂窝检索", "全网检索"]
    assert "美食专题" in names and "避坑专题" in names and "路线专题" in names and "景点专题" in names
    spot_q = {n: q for q, n in qs}["景点专题"]
    assert "成都" in spot_q and "休闲 美食" in spot_q


def test_guide_queries_without_optional_hints():
    """月份/风格缺省时查询仍完整构造（占位符不残留 None）。"""
    qs = _guide_queries("汉中", "", "")
    assert len(qs) == 7
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
