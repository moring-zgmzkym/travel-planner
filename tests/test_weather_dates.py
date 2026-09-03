"""天气日期兜底单测：travel_dates 缺失时 near_term_dates 的推近端行为。"""

from datetime import date

from tripmate.tools.weather import near_term_dates

_TODAY = date(2026, 9, 3)  # 预报窗 [09-04, 09-18]


def test_range_inside_window_clamped_to_near_end():
    """「9月1日-9月7日中的3天」：区间起点已过去，夹取到预报窗内取前 3 天。"""
    assert near_term_dates("9月1日-9月7日中的3天", 3, today=_TODAY) == [
        "2026-09-04", "2026-09-05", "2026-09-06"]


def test_range_outside_window_returns_empty():
    """「10月1日-10月7日」完全在预报窗外：返回空，维持不编造现状（不用错期天气冒充）。"""
    assert near_term_dates("10月1日-10月7日", 3, today=_TODAY) == []


def test_no_dates_falls_back_to_tomorrow():
    """「近期」等无日期表述：明天起 days 天。"""
    assert near_term_dates("近期出行", 3, today=_TODAY) == [
        "2026-09-04", "2026-09-05", "2026-09-06"]


def test_long_range_truncated_to_days():
    """区间远长于 days 时按预报窗起点截断到 days 天。"""
    assert near_term_dates("9月1日-9月20日", 2, today=_TODAY) == [
        "2026-09-04", "2026-09-05"]
