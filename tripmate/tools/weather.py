"""天气适配层（§4.4）：Open-Meteo 真实预报（免 Key）优先，降级模拟参考值（§7 降级 4）。"""

from __future__ import annotations

import httpx

from ..config import ALLOW_MOCK_FALLBACK, WeatherConfig
from ..mocks.data import mock_weather
from .resilience import ServiceUnavailable, with_retry

WMO = {
    0: "晴", 1: "多云转晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "毛毛雨", 55: "毛毛雨", 61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "阵雨", 82: "暴雨",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷阵雨伴冰雹",
}


async def query_weather(city: str, dates: list[str]) -> dict:
    """出行日期天气预报。非关键路径：失败时行程编排不做天气调整，草稿标注暂缺（§7）。"""
    try:
        async def _geo() -> dict:
            async with httpx.AsyncClient(timeout=WeatherConfig.TIMEOUT_S) as client:
                r = await client.get(WeatherConfig.GEO_URL,
                                     params={"name": city, "count": 1, "language": "zh"})
                r.raise_for_status()
                return r.json()

        geo = await with_retry(_geo, retries=1, what="城市地理编码")
        hits = geo.get("results") or []
        if not hits:
            raise ServiceUnavailable(f"未找到城市「{city}」的坐标")
        lat, lon = hits[0]["latitude"], hits[0]["longitude"]

        async def _forecast() -> dict:
            async with httpx.AsyncClient(timeout=WeatherConfig.TIMEOUT_S) as client:
                r = await client.get(WeatherConfig.BASE_URL, params={
                    "latitude": lat, "longitude": lon,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min",
                    "timezone": "auto", "forecast_days": 16,
                })
                r.raise_for_status()
                return r.json()

        fc = await with_retry(_forecast, retries=1, what="天气预报")
        daily = fc.get("daily") or {}
        by_date = {}
        for i, d in enumerate(daily.get("time", [])):
            # Open-Meteo 对个别日期可能返回 null 温度，防御性处理
            t_max, t_min = daily["temperature_2m_max"][i], daily["temperature_2m_min"][i]
            code = daily["weather_code"][i]
            if t_max is None or t_min is None or code is None:
                by_date[d] = {"day_text": "暂无预报", "temp_min": None, "temp_max": None}
                continue
            by_date[d] = {
                "day_text": WMO.get(code, "未知"),
                "temp_max": round(t_max),
                "temp_min": round(t_min),
            }
        days = []
        for d in dates:
            info = by_date.get(d)
            days.append({
                "date": d,
                **(info or {"day_text": "超出预报范围", "temp_min": None, "temp_max": None}),
            })
        return {"city": city, "source": "Open-Meteo（真实预报）", "reference_only": False, "days": days}
    except (ServiceUnavailable, httpx.HTTPError, KeyError):
        if not ALLOW_MOCK_FALLBACK:
            raise ServiceUnavailable("天气查询失败且不允许降级")
        return mock_weather(dates, city)
