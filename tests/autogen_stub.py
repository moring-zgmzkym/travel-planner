"""autogen 缺失环境的最小 stub（仅供 import 冒烟类测试使用；有真 autogen 时不安装）。"""

import sys
import types


def stub_autogen() -> None:
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

    _mod("autogen_ext")
    _mod("autogen_ext.models")
    _mod("autogen_ext.models.openai").OpenAIChatCompletionClient = type(
        "OpenAIChatCompletionClient", (), {"__init__": lambda self, *a, **kw: None})

    ac = _mod("autogen_agentchat")
    ac.agents = _mod("autogen_agentchat.agents")
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
