"""攻略摘要模型 raw 字段回归（Tavily 真实通道内容不得在写黑板时丢失）。"""

from tripmate.models import GuideDigestItem


def test_real_digest_keeps_raw_fields():
    # search.py 真实通道产出含 raw_answer/raw_titles/raw_urls → 模型必须原样接收
    d = {"source_name": "小红书（Tavily 搜索摘要级）", "source_url": "https://example.com/x",
         "fetched_at": "2026-08-29 15:00",
         "raw_answer": "汉中石门栈道是陕南代表性景点……",
         "raw_titles": ["汉中石门栈道攻略"], "raw_urls": ["https://example.com/y"],
         "reference_only": False}
    m = GuideDigestItem(**d)
    assert m.raw_answer.startswith("汉中石门栈道")
    assert m.raw_titles == ["汉中石门栈道攻略"]
    assert m.raw_urls == ["https://example.com/y"]
    assert m.spots == [] and m.foods == []  # 结构化字段由 Researcher 整理或保持为空


def test_mock_digest_raw_fields_default_empty():
    # mock 通道条目（仅结构化四元）raw 字段为默认空值，不互相污染
    m = GuideDigestItem(source_name="模拟", source_url="", fetched_at="t",
                        spots=["石门栈道"], foods=["热面皮"], warnings=["备雨具"])
    assert m.raw_answer == "" and m.raw_titles == [] and m.raw_urls == []
    assert m.spots == ["石门栈道"] and m.reference_only is False
