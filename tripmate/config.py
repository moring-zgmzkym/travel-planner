"""集中式配置：全部经环境变量（.env）注入，未配置的外部依赖自动走降级路径（企划书 §7）。"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

OUTPUT_DIR = BASE_DIR / "outputs"
IMAGE_DIR = OUTPUT_DIR / "images"
LOG_DIR = BASE_DIR / "logs"
for _d in (OUTPUT_DIR, IMAGE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


class LLMConfig:
    """LLM：OpenAI 兼容接口。企划书指定硅基流动+千问系列，任意兼容服务可无痛替换（§3.7）。"""

    API_KEY: str = _env("LLM_API_KEY")
    BASE_URL: str = _env("LLM_BASE_URL", "https://api.siliconflow.cn/v1")
    MODEL: str = _env("LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct")
    MAX_TOKENS: int = int(_env("LLM_MAX_TOKENS", "8192"))
    TEMPERATURE: float = float(_env("LLM_TEMPERATURE", "0.3"))
    # 单次调用超时与重试（秒/次）。预算过宽会让限流通道把一次调用拖到 20 分钟，
    # 表现为"Agent 不及时返回 / Chatter 不回复"——失败应尽快暴露给护栏与用户。
    TIMEOUT_S: float = float(_env("LLM_TIMEOUT_S", "150"))
    MAX_RETRIES: int = int(_env("LLM_MAX_RETRIES", "1"))


class SearchConfig:
    """搜索引擎（Tavily）。未配置 Key 时降级为内置模拟攻略数据（标注参考值）。"""

    TAVILY_API_KEY: str = _env("TAVILY_API_KEY")
    TIMEOUT_S: float = 30.0
    RETRIES: int = 2
    RETRY_DELAY_S: float = 5.0


class McpConfig:
    """MCP 外部能力。Key/命令未配置或服务不可达时，逐项降级为模拟数据（§7 降级方案）。"""

    # 高德官方 MCP（SSE/Streamable HTTP），需「Web 服务」类型 Key
    AMAP_API_KEY: str = _env("AMAP_API_KEY")
    AMAP_MCP_URL: str = _env("AMAP_MCP_URL", "https://mcp.amap.com/sse")
    # 社区 12306-MCP（stdio，默认拉起 npm 包；Node >= 18）
    MCP_12306_COMMAND: str = _env("MCP_12306_COMMAND", "npx -y 12306-mcp")
    # 酒店 MCP（社区实现，覆盖不全，默认关闭走模拟）
    MCP_HOTEL_URL: str = _env("MCP_HOTEL_URL")
    TIMEOUT_S: float = 30.0
    RETRIES: int = 2
    RETRY_DELAY_S: float = 5.0


class WeatherConfig:
    """天气：默认走 Open-Meteo 免费无 Key 真实数据源；网络不可达时降级模拟。"""

    BASE_URL: str = _env("WEATHER_BASE_URL", "https://api.open-meteo.com/v1/forecast")
    GEO_URL: str = _env("WEATHER_GEO_URL", "https://geocoding-api.open-meteo.com/v1/search")
    TIMEOUT_S: float = 20.0


class BudgetConfig:
    """成本控制（§2.3）：单次完整规划 token 上限。"""

    TOKEN_LIMIT: int = int(_env("TOKEN_BUDGET", "200000"))
    MAX_DRAFT_ROUNDS: int = 3          # 草稿确认循环上限（§4.5）
    MAX_ASK_ROUNDS: int = 3            # 信息追问上限（§2.1）
    MAX_TEAM_TURNS: int = 30           # 单阶段群聊轮次安全上限（风险 #5）
    MAX_CONSECUTIVE_SPEAKER: int = 2   # 单 Agent 连续发言上限（风险 #5）


class ServerConfig:
    HOST: str = _env("HOST", "127.0.0.1")
    PORT: int = int(_env("PORT", "8000"))
    HEARTBEAT_S: float = 30.0          # WebSocket 心跳（§2.3）
    STATUS_REPLAY: int = 80            # 断线重连补发的最近状态条数（风险 #7）


# 是否允许降级（关闭后外部服务失败直接报错，用于演示降级开关）
ALLOW_MOCK_FALLBACK: bool = _env("ALLOW_MOCK_FALLBACK", "1") not in ("0", "false", "False")
