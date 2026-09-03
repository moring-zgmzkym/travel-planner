"""车票适配层（§4.4 MCP 专项 Agent 工具）：12306-MCP 真实查询 + 打分勾选 + 降级。"""

from __future__ import annotations

from datetime import datetime

from ..config import ALLOW_MOCK_FALLBACK
from ..mocks.data import (
    mock_flight_tickets,
    mock_transport_estimate_km,
    mock_train_tickets,
)
from .mcp_client import ServiceUnavailable, train_session
from .resilience import with_retry

GAS_PRICE_PER_KM = 0.8      # 自驾油费+过路估算（元/km，参考值）
BUS_PRICE_PER_KM = 0.35     # 长途大巴（元/km，参考值）


async def query_tickets(origin: str, destination: str, dates: list[str], mode: str) -> dict:
    """车票查询入口。高铁→12306-MCP；飞机→无 MCP 走搜索摘要级参考；自驾/大巴→里程估算。"""
    date = dates[0] if dates else datetime.now().strftime("%Y-%m-%d")
    if mode == "自驾":
        km = mock_transport_estimate_km(origin, destination)
        return {"mode": "mock", "notice": "自驾出行：交通费按往返里程 × 油价估算（参考值）",
                "candidates": [_estimate_ticket("自驾（往返油费+过路）", km * 2 * GAS_PRICE_PER_KM)],
                "transport_kind": "estimate"}
    if mode == "长途大巴":
        km = mock_transport_estimate_km(origin, destination)
        return {"mode": "mock", "notice": "长途大巴：按往返里程 × 票价率估算（参考值）",
                "candidates": [_estimate_ticket("长途大巴（往返）", km * 2 * BUS_PRICE_PER_KM)],
                "transport_kind": "estimate"}
    if mode == "飞机":
        return {"mode": "mock", "notice": "暂无机票 MCP，按 §7 降级走搜索摘要级参考数据（价格仅供参考）",
                "candidates": mock_flight_tickets(origin, destination, date), "transport_kind": "flight"}

    # 高铁：真实 12306-MCP 优先
    try:
        session = train_session()

        async def _query() -> list:
            return await session.call(
                ("ticket",),
                {"from": origin, "to": destination, "date": date, "fromStation": origin,
                 "toStation": destination, "departure_date": date, "format": "json"},
                what="12306 车票查询")

        raw = await with_retry(_query, retries=0, what="12306 车票查询")
        candidates = _normalize_12306(raw, origin, destination, date)
        if candidates:
            return {"mode": "real", "candidates": candidates, "transport_kind": "train"}
        raise ServiceUnavailable("12306-MCP 返回为空")
    except ServiceUnavailable as e:
        if not ALLOW_MOCK_FALLBACK:
            raise
        return {"mode": "mock", "notice": f"12306 通道暂不可用（{e}），已切换模拟班次表（参考值，§7 降级方案）",
                "candidates": mock_train_tickets(origin, destination, date), "transport_kind": "train"}


def _row_price(r: dict, pick) -> float:
    """票价提取：12306-mcp(format=json) 把票价放在嵌套 prices[]（按席位），
    优先取二等座（O），否则取最低可用价；其他社区实现为标量字段，沿用模糊匹配。"""
    prices = r.get("prices")
    if isinstance(prices, list):
        candidates = []
        for p in prices:
            if not isinstance(p, dict):
                continue
            try:
                v = float(p.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                candidates.append((str(p.get("seat_type_code") or ""), str(p.get("seat_name") or ""), v))
        if candidates:
            second = [v for c, name, v in candidates if c == "O" or "二等" in name]
            return min(second) if second else min(v for _, _, v in candidates)
        return 0.0
    price = pick("price", "secondClass", "edz", "swz") or ""
    try:
        return float(str(price).replace("¥", "").replace("元", "").replace(",", "")) if price else 0.0
    except ValueError:
        return 0.0


def _normalize_12306(raw: object, origin: str, destination: str, date: str) -> list[dict]:
    """尽力解析社区 MCP 的返回结构（字段名模糊匹配）。"""
    rows = raw if isinstance(raw, list) else []
    if isinstance(raw, dict):
        rows = raw.get("tickets") or raw.get("data") or raw.get("result") or []
        if isinstance(rows, dict):
            rows = [rows]
    out = []
    for r in rows[:8]:
        if not isinstance(r, dict):
            continue
        def pick(*keys: str) -> str:
            for k in keys:
                for rk, rv in r.items():
                    if k.lower() in rk.lower() and rv not in (None, ""):
                        return str(rv)
            return ""
        # 12306-mcp 行字段带下划线（start_train_code/start_time/arrive_time/lishi），
        # pick 是子串匹配，fragment 必须含下划线才能先命中正确键、又不误吞 arrive_date
        train_no = pick("train_code", "trainNo", "train", "code")
        price_f = _row_price(r, pick)
        dep, arr = pick("start_time", "depart", "startTime"), pick("arrive_time", "arrive", "arriveTime")
        dur = pick("duration", "lishi", "历时", "spend")
        if not train_no or price_f <= 0:
            continue
        # 12306 深链：站点电报码（MCP 返回自带）才能让余票页预填并自动查询；缺失回退站名格式
        from_code = str(r.get("from_station_telecode") or "").strip()
        to_code = str(r.get("to_station_telecode") or "").strip()
        if from_code and to_code:
            link = (f"https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc"
                    f"&fs={origin},{from_code}&ts={destination},{to_code}&date={date}&flag=N,N")
        else:
            link = f"https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc&fs={origin}&ts={destination}&date={date}"
        out.append({
            "train_no": train_no, "depart_time": dep or "--:--", "arrive_time": arr or "--:--",
            "duration_min": _parse_duration(dur, dep, arr), "price": price_f,
            "link": link,
            "from_telecode": from_code, "to_telecode": to_code,
            "source": "12306-MCP（实时数据）", "reference_only": False,
        })
    return out


def _parse_duration(dur: str, dep: str, arr: str) -> int:
    try:
        if dur:
            parts = dur.replace(":", "小时").split("小时")
            h = int(parts[0]) if parts[0].isdigit() else 0
            m = int(parts[1].replace("分", "")) if len(parts) > 1 and parts[1].replace("分", "").isdigit() else 0
            return h * 60 + m
        if dep and arr and "--" not in dep + arr:
            d = datetime.strptime(dep, "%H:%M")
            a = datetime.strptime(arr, "%H:%M")
            return int((a - d).total_seconds() // 60) % (24 * 60)
    except ValueError:
        pass
    return 0


def _estimate_ticket(label: str, price: float) -> dict:
    return {"train_no": label, "depart_time": "按行程安排", "arrive_time": "按行程安排",
            "duration_min": 0, "price": round(price, 1),
            "link": "", "source": "里程估算（参考值）", "reference_only": True}


def score_and_select(candidates: list[dict], party_size: int = 1) -> list[dict]:
    """车票打分勾选（§4.4）：score = 0.5×时间合理度 + 0.3×价格 + 0.2×历时；top1 自动勾选。"""
    if not candidates:
        return []
    prices = [c["price"] for c in candidates if c["price"] > 0] or [1]
    durs = [c["duration_min"] for c in candidates if c["duration_min"] > 0] or [1]
    p_min, p_max, d_min, d_max = min(prices), max(prices), min(durs), max(durs)

    def norm(v: float, lo: float, hi: float, invert: bool) -> float:
        if hi <= lo:
            return 1.0
        t = (v - lo) / (hi - lo)
        return 1 - t if invert else t

    for c in candidates:
        t_score = _time_reasonableness(c.get("depart_time", ""))
        p_score = norm(c["price"], p_min, p_max, invert=True) if c["price"] > 0 else 0.5
        d_score = norm(c["duration_min"], d_min, d_max, invert=True) if c["duration_min"] > 0 else 0.5
        c["score"] = round(0.5 * t_score + 0.3 * p_score + 0.2 * d_score, 3)
    ranked = sorted(candidates, key=lambda c: -c["score"])
    top = ranked[0]
    top["selected"] = True
    top["reason"] = (f"综合评分最高（{top['score']}）：出发时间{_depart_window(top['depart_time'])}，"
                     f"价格 {top['price']} 元，历时 {top['duration_min']} 分钟")
    return ranked


def _time_reasonableness(depart: str) -> float:
    """出发 07:00-10:00 最优（1.0），窗口外按偏离小时数线性衰减。"""
    try:
        h, m = depart.split(":")
        t = int(h) + int(m) / 60
    except (ValueError, AttributeError):
        return 0.5
    if 7 <= t <= 10:
        return 1.0
    dist = min(abs(t - 7), abs(t - 10))
    return max(0.0, 1.0 - dist / 8)


def _depart_window(depart: str) -> str:
    return f"{depart}（07:00-10:00 黄金窗口）" if _time_reasonableness(depart) == 1.0 else depart
