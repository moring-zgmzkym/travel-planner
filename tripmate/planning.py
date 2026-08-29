"""规划纯逻辑（可单测）：变更影响分析（§5.3）、预算核算（§4.5）、草稿校验、行程工具函数。"""

from __future__ import annotations

from typing import Any

from .mocks.data import kb_for_city, spot_ticket_price
from .models import Draft, TravelProfile

# 每日景点数（§2.2 行程节奏）
PACE_SPOTS = {"快": (4, 5), "中": (3, 3), "慢": (2, 2)}
RESERVE_RATE = 0.08   # 备用金比例
FOOD_FALLBACK = 120   # 未收录城市餐饮日均值

# 字段 → 受影响环节（§5.3 变更影响分析规则）
FIELD_IMPACT: dict[str, set[str]] = {
    "destination": {"guides", "tickets", "hotels", "weather", "itinerary"},   # 全量重跑
    "origin": {"tickets", "itinerary"},
    "travel_dates": {"tickets", "hotels", "weather", "itinerary"},            # 车票+酒店+天气重查，攻略复用
    "travel_mode": {"tickets", "itinerary"},
    "date_text": {"tickets", "weather"},
    "budget": {"hotels", "itinerary"},                                        # 预算 → 仅酒店重查
    "budget_max": {"itinerary"},
    "style": {"itinerary"},                                                   # 风格 → 仅行程重排
    "party_size": {"tickets", "hotels", "itinerary"},
    "hotel.location_pref": {"hotels"},
    "hotel.price_range": {"hotels"},
    "hotel.min_star": {"hotels"},
    "must_visit": {"itinerary"},
    "food_restrictions": {"itinerary"},
    "pace": {"itinerary"},
    "special_needs": {"itinerary"},
}


def analyze_impact(changed_fields: list[str]) -> set[str]:
    """变更字段清单 → 受影响环节集合。"""
    affected: set[str] = set()
    for f in changed_fields:
        affected |= FIELD_IMPACT.get(f, {"itinerary"})
    return affected


def nights_of(days: int) -> int:
    return max(days - 1, 0)


def compute_budget(profile: TravelProfile, draft: Draft | None) -> dict[str, Any]:
    """预算核算（§4.5）：交通 + 住宿×晚数 + 门票 + 餐饮估算 + 备用金；>90% 预警、超最大预算建议压缩。

    预算为全团口径；门票/餐饮按人数倍乘（§2.2 同行人数）。
    """
    basic, detail = profile.basic_info, profile.detail_info
    party = detail.party_size or basic.party_size or 1

    # 交通：已勾选车票（estimate 类已是往返价；train/flight 为单程 → ×2）
    transport = 0.0
    transport_note = "未勾选车票"
    selected = [t for t in profile.tickets if t.selected]
    if selected:
        t = selected[0]
        if "往返" in t.train_no:  # 自驾/大巴估算票已是往返合计价
            transport = t.price
            transport_note = f"{t.train_no}（往返合计，全团）"
        else:
            transport = t.price * 2 * party
            transport_note = f"{t.train_no} 往返 × {party} 人"

    # 住宿：已勾选酒店 × 晚数 × 1 间
    hotel_cost = 0.0
    hotel_note = "未勾选酒店"
    selected_h = [h for h in profile.hotels if h.selected]
    if selected_h:
        h = selected_h[0]
        nights = nights_of(basic.days or 1)
        hotel_cost = h.price_per_night * nights
        hotel_note = f"{h.name} {h.price_per_night}×{nights} 晚"

    # 门票：草稿景点清单 × 人数
    ticket_cost = 0.0
    spot_items: list[dict] = []
    if draft:
        seen: set[str] = set()
        for day in draft.days:
            for s in day.spots:
                if s in seen:
                    continue
                seen.add(s)
                p = spot_ticket_price(s)
                ticket_cost += p * party
                spot_items.append({"spot": s, "price": p, "per_party": round(p * party, 1)})

    # 餐饮：城市日均 × 天数 × 人数
    food_daily = kb_for_city(basic.destination or "").get("food_cost_per_day", FOOD_FALLBACK)
    food_cost = food_daily * (basic.days or 1) * party

    subtotal = transport + hotel_cost + ticket_cost + food_cost
    reserve = round(subtotal * RESERVE_RATE, 1)
    total = round(subtotal + reserve, 1)

    budget = basic.budget or 0
    budget_max = basic.budget_max or (budget * 1.2 if budget else 0)
    warnings: list[str] = []
    if budget_max and total > budget_max:
        warnings.append(f"总预算 {total} 元已超出最大预算 {budget_max} 元，建议压缩：优先下调住宿标准/减少收费景点")
    elif budget and total > budget * 0.9:
        warnings.append(f"总预算 {total} 元已占用预算 {budget} 元的 {total / budget:.0%}（>90% 预警）")

    items = [
        {"item": "交通", "note": transport_note, "amount": round(transport, 1)},
        {"item": "住宿", "note": hotel_note, "amount": round(hotel_cost, 1)},
        {"item": "门票", "note": f"{len(spot_items)} 个景点 × {party} 人", "amount": round(ticket_cost, 1)},
        {"item": "餐饮", "note": f"{food_daily}/人/日 × {(basic.days or 1)} 天 × {party} 人", "amount": round(food_cost, 1)},
        {"item": "备用金", "note": f"小计 × {RESERVE_RATE:.0%}", "amount": reserve},
    ]
    return {
        "items": items, "spot_items": spot_items, "total": total,
        "budget": budget, "budget_max": budget_max,
        "occupancy": round(total / (budget or budget_max), 3) if (budget or budget_max) else None,
        "warnings": warnings,
    }


def validate_draft(profile: TravelProfile, draft: Draft) -> list[str]:
    """草稿硬校验：天数一致、必经景点排入、节奏景点数（±1 容差）。返回错误清单（空 = 通过）。"""
    errors: list[str] = []
    basic, detail = profile.basic_info, profile.detail_info
    days_needed = basic.days or 0
    if len(draft.days) != days_needed:
        errors.append(f"行程天数 {len(draft.days)} 与要求 {days_needed} 不一致")
    # 仅当画像给出完整逐日日期序列时才逐日校验（[出发日,返程日] 等区间形式不做逐日比对）
    if basic.travel_dates and len(basic.travel_dates) == days_needed == len(draft.days):
        for i, d in enumerate(draft.days):
            want = basic.travel_dates[i]
            if want and d.date != want:
                errors.append(f"第 {i + 1} 天日期 {d.date} 应为 {want}")
    all_spots: set[str] = set()
    for day in draft.days:
        for s in day.spots:
            all_spots.add(s)
    for must in detail.must_visit:
        if not any(must in s or s in must for s in all_spots):
            errors.append(f"必经景点「{must}」未排入行程")
    pace = detail.pace or "中"
    lo, hi = PACE_SPOTS.get(pace, (3, 3))
    for i, day in enumerate(draft.days):
        n = len(day.spots)
        if not (lo - 1 <= n <= hi + 1):
            errors.append(f"第 {i + 1} 天景点数 {n} 偏离「{pace}」节奏（参考 {lo}-{hi} 个）")
    return errors


def draft_summary_text(draft: Draft) -> str:
    """草稿的群聊/预览摘要文本。"""
    lines = []
    for i, day in enumerate(draft.days):
        lines.append(f"第{i + 1}天 {day.date}：上午 {day.morning}｜下午 {day.afternoon}｜晚上 {day.evening}")
    return "\n".join(lines)
