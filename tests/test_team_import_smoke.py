"""team.py / gateway 导入冒烟（方向二集成验证）：

本机可能缺 autogen（队友机器必有）。策略：已装 autogen 就测真实导入；
未装则注入最小 stub 后导入——足以捕获 import 期的 NameError/拼写/循环依赖错误。
"""

import importlib
import sys
import types

import tripmate.designer as dmod  # noqa: E402 — 无 autogen 环境也必须可导入（本文件的第一道验证）


def _stub_autogen() -> None:
    """注入 import 期所需的最小 API 面（仅类定义，不实例化）。"""
    from pydantic import BaseModel

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    core = _mod("autogen_core")
    core.CancellationToken = type("CancellationToken", (), {})
    core.Component = type("Component", (), {"__class_getitem__": classmethod(lambda cls, item: cls)})

    core_models = _mod("autogen_core.models")

    class _ModelInfo(BaseModel):
        vision: bool = False
        function_calling: bool = True
        json_output: bool = False
        structured_output: bool = False
        family: str = "unknown"

    class _RequestUsage(BaseModel):
        prompt_tokens: int = 0
        completion_tokens: int = 0

    core_models.ModelInfo = _ModelInfo
    core_models.RequestUsage = _RequestUsage
    core_models.ModelCapabilities = dict  # type: ignore
    core_models.LLMMessage = object
    core_models.CreateResult = type("CreateResult", (), {})
    core_models.ChatCompletionClient = type("ChatCompletionClient", (), {})

    core_tools = _mod("autogen_core.tools")
    core_tools.Tool = type("Tool", (), {})
    core_tools.ToolSchema = dict  # type: ignore

    ext = _mod("autogen_ext")
    ext_models = _mod("autogen_ext.models")
    _mod("autogen_ext.models.openai").OpenAIChatCompletionClient = type(
        "OpenAIChatCompletionClient", (), {})

    ac = _mod("autogen_agentchat")
    ac.agents = _mod("autogen_agentchat.agents")
    # 接受任意构造参数（build_chatter/TeamRunner 模块级实例化时会真正调用）
    _Agent = type("AssistantAgent", (), {"__init__": lambda self, *a, **kw: None})
    ac.agents.AssistantAgent = _Agent
    ac.conditions = _mod("autogen_agentchat.conditions")
    ac.conditions.MaxMessageTermination = type("MaxMessageTermination", (), {})
    ac.conditions.TextMentionTermination = type("TextMentionTermination", (), {})
    ac.messages = _mod("autogen_agentchat.messages")
    for cls in ("BaseAgentEvent", "BaseChatMessage", "ToolCallExecutionEvent",
                "ToolCallRequestEvent", "StopMessage", "ThoughtEvent"):
        setattr(ac.messages, cls, type(cls, (), {}))
    ac.teams = _mod("autogen_agentchat.teams")
    ac.teams.SelectorGroupChat = type("SelectorGroupChat", (), {})
    ac.base = _mod("autogen_agentchat.base")
    ac.base.TerminatedException = type("TerminatedException", (RuntimeError,), {})


try:
    importlib.import_module("autogen_agentchat")
    _STUBBED = False
except ImportError:
    _stub_autogen()
    _STUBBED = True


def test_team_imports_and_constants():
    from tripmate import team

    assert team.AGENT_DESIGNER == "Designer"
    assert "PLAN_PDF" in team.SPEAKER  # 状态机本身零改动
    assert callable(team._deliver_final)
    assert callable(team._deliver_designer)
    assert "classic" in team.REGISTRY


def test_gateway_imports_designer_meta(monkeypatch):
    """gateway 模块级实例化 Session → build_chatter → get_model_client（无 .env 会 RuntimeError，
    先于方向二即如此），打桩后再导入，验证 designer 伪模板元数据接线。"""
    import tripmate.llm as llm_mod
    monkeypatch.setattr(llm_mod, "get_model_client", lambda: None)
    from tripmate.designer import DESIGNER_TEMPLATE_META
    importlib.reload(importlib.import_module("tripmate.gateway.app"))
    from tripmate.gateway import app

    assert DESIGNER_TEMPLATE_META["name"] == "designer"
    assert callable(app.templates)


def test_designer_module_autogen_free():
    """designer.py 顶层 import 链不依赖 autogen（无 autogen 环境可全量单测的前提，AST 结构检查）。"""
    import ast
    from pathlib import Path

    src = Path(dmod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:  # 仅顶层：函数体内的延迟导入（AutoGen 工厂）是允许的
        if isinstance(node, ast.Import):
            assert not any(a.name.startswith("autogen") for a in node.names), "designer 顶层不得 import autogen"
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("autogen"), "designer 顶层不得 from autogen import"
    assert callable(dmod.designer_chain)
    assert dmod.DESIGNER_TIMEOUT_S == 600
