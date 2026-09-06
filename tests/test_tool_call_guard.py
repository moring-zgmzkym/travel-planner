"""Chatter 工具调用文本化检测回归（hy3-free <tool_sep:...> 失效模式，2026-08-29 e2e 实测）。"""

from tripmate.chatter import clean_reply
from tripmate.session import _missed_tool_call


def test_serialized_tool_call_detected():
    # 实测失效样例：模型把工具调用参数序列化成文本，正则必须命中
    assert _missed_tool_call(
        "submit_draft_feedback<tool_sep:6124c78e> <arg_key:6124c78e>confirmed true")
    assert _missed_tool_call("submit_draft_feedback confirmed truetype")  # 第二轮实测：纯文本无标记变体
    assert _missed_tool_call("start_planning")
    assert _missed_tool_call("<tool_calls:start_planning></tool_calls>")
    assert _missed_tool_call("<arg_value>{}<arg_value>")
    assert _missed_tool_call("save_travel_info")


def test_normal_reply_not_flagged():
    # 正常中文回复（含提及工具名）不得误报，否则会多跑一轮无谓重试
    assert not _missed_tool_call("好的，已确认草稿，正在为您生成最终行程 PDF。")
    assert not _missed_tool_call("信息已齐备，旅行规划团队已在后台启动，请稍候。")
    assert not _missed_tool_call("")


def test_clean_reply_strips_new_markers():
    dirty = "submit_draft_feedback<tool_sep:abc> <arg_key:abc>confirmed true"
    cleaned = clean_reply(dirty)
    assert "<tool_sep" not in cleaned and "<arg_key" not in cleaned


def test_serialized_tool_call_with_leading_punctuation_detected():
    """2026-09-05 e2e 实测新变体：前导顿号 + 函数调用形态的文本化输出必须命中，
    否则反馈永不真正提交、修订流程卡死（本次运行事故的回归）。"""
    assert _missed_tool_call(
        "、submit_draft_feedback(confirm=false, feedback=\"第2天调整为都江堰一日游\")")
    assert _missed_tool_call("，start_planning")
    assert _missed_tool_call("。get_travel_profile")
    # 正常中文回复仍不误报
    assert not _missed_tool_call("好的，已确认草稿。")
    assert not _missed_tool_call("您的行程已生成，请查收。")


def test_feedback_intent_matches_textualized_call():
    """反馈意图断言覆盖文本化调用形态（专用 nudge 接管）。"""
    from tripmate.session import _FEEDBACK_INTENT
    assert _FEEDBACK_INTENT.search('submit_draft_feedback(confirm=false, feedback="x")')
    assert _FEEDBACK_INTENT.search("把这条修改意见提交给规划团队")
    assert not _FEEDBACK_INTENT.search("今天天气不错")


def test_function_style_textualization_detected():
    """2026-09-06 e2e 实测第三种文本化变体（GLM）：<function=工具名>\n<parameter=...>
    XML 风格标记——必须命中检测并从用户可见回复中清洗。"""
    dirty = '<function=save_travel_info>\n<parameter=detail_info>\n{"must_visit": ["都江堰"]}'
    assert _missed_tool_call(dirty)
    assert _missed_tool_call("<function=submit_draft_feedback>")
    cleaned = clean_reply(dirty)
    assert "<function=" not in cleaned and "<parameter=" not in cleaned
