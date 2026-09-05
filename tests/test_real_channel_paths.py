"""外部通道真实路径回归（2026-09-04 补盲区）：search_guides / query_weather 的
共享客户端结构与单路故障隔离——此前测试全部走 mock 通道（TAVILY_API_KEY 置空），
真实分支零覆盖，共享客户端/信号量/geo→预报串联结构无锁定。

fake httpx 客户端全程离线，不耗真实额度。
"""

import asyncio

import httpx

from tripmate.config import SearchConfig, WeatherConfig
from tripmate.tools.search import search_guides
from tripmate.tools.weather import query_weather


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_search_guides_real_path_single_client_and_isolation(monkeypatch):
    """真实通道：7 路全部发出、共享 1 个客户端、单路故障只降级该路。"""
    monkeypatch.setattr(SearchConfig, "TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(SearchConfig, "RETRY_DELAY_S", 0.001)  # 故障路重试退避不实睡
    seen_queries: list[str] = []
    instances = {"n": 0}

    class FakeClient:
        def __init__(self, **kwargs):
            instances["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            seen_queries.append(json["query"])
            if "避坑" in json["query"]:  # 单路故障（重试后仍失败）
                raise httpx.ConnectError("单路故障")
            return _FakeResponse({
                "answer": f"{json['query']} 的摘要",
                "results": [{"url": "https://www.chinadaily.com/x", "title": "标题"}],
            })

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def main():
        return await search_guides("西安", "9月", style_hint="休闲")

    r = asyncio.run(main())
    assert r["mode"] == "real"
    assert len(set(seen_queries)) == 7   # 7 路全部发出（故障路含重试共 3 次提交）
    assert instances["n"] == 1           # 7 路共享 1 个客户端（连接复用锁定）
    assert len(r["digest"]) == 6         # 故障路降级跳过，其余 6 路合并
    assert all(d["reference_only"] is False for d in r["digest"])


def test_query_weather_real_path_single_client(monkeypatch):
    """真实通道：geo→预报串联共享 1 个客户端，逐日字段正确。"""
    import tripmate.tools.weather as weather_mod
    monkeypatch.setattr(weather_mod, "ALLOW_MOCK_FALLBACK", True)
    instances = {"n": 0}
    urls_seen: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs):
            instances["n"] += 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            urls_seen.append(url)
            if url == WeatherConfig.GEO_URL:
                return _FakeResponse({"results": [{"latitude": 34.26, "longitude": 108.94}]})
            return _FakeResponse({"daily": {
                "time": ["2026-10-01"],
                "weather_code": [0],
                "temperature_2m_max": [30.0],
                "temperature_2m_min": [18.0],
            }})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def main():
        return await query_weather("西安", ["2026-10-01"])

    res = asyncio.run(main())
    assert res["source"] == "Open-Meteo（真实预报）" and res["reference_only"] is False
    assert res["days"] == [{"date": "2026-10-01", "day_text": "晴", "temp_max": 30, "temp_min": 18}]
    assert instances["n"] == 1  # geo + 预报（各带重试）共享 1 个客户端
    assert urls_seen == [WeatherConfig.GEO_URL, WeatherConfig.BASE_URL]


def test_query_weather_degrades_when_geocode_empty(monkeypatch):
    """地理编码空结果 → ServiceUnavailable → mock 降级（reference_only 标注）。"""
    import tripmate.tools.weather as weather_mod
    monkeypatch.setattr(weather_mod, "ALLOW_MOCK_FALLBACK", True)  # 不读用户 .env，保证确定性

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return _FakeResponse({"results": []})

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    async def main():
        return await query_weather("西安", ["2026-10-01"])

    res = asyncio.run(main())
    assert res["reference_only"] is True and res["source"].startswith("模拟天气")
    assert res["days"][0]["date"] == "2026-10-01"
