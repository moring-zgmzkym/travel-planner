"""天气适配层（§4.4）：Open-Meteo 真实预报（免 Key）优先，降级模拟参考值（§7 降级 4）。"""

from __future__ import annotations

import re
from datetime import date, timedelta

import httpx

from ..config import ALLOW_MOCK_FALLBACK, WeatherConfig
from ..mocks.data import mock_weather
from .resilience import ServiceUnavailable, with_retry

# Open-Meteo 预报窗上限（查询参数 forecast_days=16）：窗外逐日一律"超出预报范围"
_FORECAST_WINDOW_DAYS = 16

# 天气词 → emoji 图案组合（关键词按优先级排列，先长后短；PDF 用 PIL 栅格化，勿直接进 Paragraph）
_WMO_EMOJI = [
    ("雷", "⛈️"), ("冰雹", "⛈️"),
    ("大雪", "❄️❄️"), ("中雪", "🌨️"), ("小雪", "🌨️"), ("雪", "🌨️"),
    ("暴雨", "⛈️"), ("大雨", "🌧️"), ("阵雨", "🌦️"), ("雨", "🌦️"),
    ("雾", "🌫️"), ("霾", "😷"),
    ("多云转晴", "🌤️"), ("多云", "⛅"), ("阴", "☁️"), ("晴", "☀️"),
]


def weather_emoji(day_text: str) -> str:
    """天气词 → emoji 图案组合（PDF 天气模块用）；未命中返回多云兜底。"""
    for key, icon in _WMO_EMOJI:
        if key in (day_text or ""):
            return icon
    return "⛅"


def outfit_advice(day_text: str, temp_max, temp_min) -> str:
    """确定性穿搭建议（纯文本，不含 emoji——emoji 由模板栅格化单独出图）。

    降水提醒优先，其余按最高温分档；昼夜温差 ≥10℃ 追加分层提醒；温度缺失给通用建议。"""
    day_text = day_text or ""
    tips: list[str] = []
    if any(k in day_text for k in ("雨", "雷")):
        tips.append("有降水，务必带伞或雨衣，穿防滑防水鞋")
    if "雪" in day_text:
        tips.append("降雪路滑，注意保暖防滑")
    t = temp_max if isinstance(temp_max, (int, float)) else None
    tmin = temp_min if isinstance(temp_min, (int, float)) else None
    if t is None:
        tips.append("气温不明，建议洋葱式分层穿搭，出发前再次查看预报")
    elif t >= 32:
        tips.append("高温酷暑：透气速干短袖，防晒霜、遮阳帽、墨镜，多次补水")
    elif t >= 27:
        tips.append("炎热：短袖配轻薄裤装，注意防晒，午间减少暴晒")
    elif t >= 21:
        tips.append("舒适：短袖或薄长袖即可，早晚备一件薄外套")
    elif t >= 14:
        tips.append("微凉：长袖加外套或卫衣，早晚温差注意添衣")
    elif t >= 6:
        tips.append("偏冷：厚外套或夹棉，内搭长袖保暖")
    else:
        tips.append("寒冷：羽绒服加保暖内搭，手套围巾齐上")
    if t is not None and tmin is not None and t - tmin >= 10:
        tips.append("昼夜温差大，建议洋葱式分层穿搭")
    return "；".join(tips) if tips else "轻装出行"


def near_term_dates(date_text: str, days: int, today: date | None = None) -> list[str]:
    """travel_dates 缺失时的天气查询日期兜底（纯函数）。

    - date_text 含「M月D日」区间：解析首末日期（年份取当前），与预报窗 [明天, 今天+15]
      求交集，取交集前 days 天；区间与预报窗无交集（如国庆行程在 9 月查询）→ 返回空，
      维持"不编造、标注超出预报范围"的现状，不用错期天气冒充；
    - 解析不到任何日期（如「近期」）→ 明天起 days 天。
    """
    today = today or date.today()
    days = max(1, days)
    win_lo, win_hi = today + timedelta(days=1), today + timedelta(days=_FORECAST_WINDOW_DAYS - 1)

    parsed: list[date] = []
    for m in re.finditer(r"(\d{1,2})月(\d{1,2})[日号]", date_text or ""):
        try:
            parsed.append(date(today.year, int(m.group(1)), int(m.group(2))))
        except ValueError:  # 非法日期（如 13月）忽略
            continue

    if parsed:
        lo, hi = min(parsed), max(parsed)
        lo, hi = max(lo, win_lo), min(hi, win_hi)
        if lo > hi:
            return []
        span = (hi - lo).days + 1
        return [(lo + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(min(days, span))]

    return [(win_lo + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]

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
