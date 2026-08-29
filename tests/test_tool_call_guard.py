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
