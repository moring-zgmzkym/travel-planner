"""TravelProfile → 唐风路书模板上下文。

只做数据整形与确定性加工（预算/穿搭/路线段间文案与 reportlab 版式逐项对齐，
功能对齐规格见 docs/pdf-template-plan.md），排版交给 Jinja2 模板。
外部/LLM 文本一律以纯文本进上下文，由模板 autoescape 转义；仅本模块自产的
SVG（内部数值/已转义插值）以 Markup 传入。所有加工失败均回退空值/占位，
不让 PDF 构建失败。
"""

from __future__ import annotations

import base64
import io
import math
import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..config import OUTPUT_DIR
from ..models import TravelProfile
from ..planning import compute_budget
from ..tools.weather import outfit_advice, weather_emoji

# 与 pdf_templates/base.py 目的地消毒规则保持一致（HTML 路径独立成包，不反向依赖 reportlab 层）
_DEST_BAD = re.compile(r'[\\/:*?"<>|\r\n]')

# 参考样张《陕西3天2晚懒人版旅行路书_图文版.pdf》实测图表色板（依次取用）
PALETTE = ["#4A599D", "#B3BEDC", "#C4593F", "#D9A441", "#94B497", "#7E8E5E"]
_STOP_BADGE = {"spot": "🏛", "meal": "🍜", "hotel": "🏨"}


def output_path(profile: TravelProfile, run_id: str) -> Path:
    """成品路径：outputs/行程计划_{消毒后目的地}_{run_id 前 8 位}.pdf（与 reportlab 路径同规则）。"""
    dest = _DEST_BAD.sub("_", (profile.basic_info.destination or "行程").strip()) or "行程"
    return OUTPUT_DIR / f"行程计划_{dest}_{(run_id or '')[:8]}.pdf"


def _safe_url(url: str) -> str:
    """仅放行 http(s) 链接（LLM/外部文本进 href 与二维码前的白名单）。"""
    u = (url or "").strip()
    return u if u.startswith(("http://", "https://")) else ""


def _img_uri(path: str, max_w: int = 1600) -> str:
    """本地图片 → data URI（超宽降采样 + JPEG 重压缩，控制 HTML 体积）；失败返回空串。"""
    if not path:
        return ""
    try:
        p = Path(path)
        if not p.exists():
            return ""
        from PIL import Image as PILImage
        with PILImage.open(p) as im:
            im = im.convert("RGB")
            if im.width > max_w:
                im = im.resize((max_w, max(1, int(im.height * max_w / im.width))))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 — 单图失败回退占位，不阻塞
        return ""


def _qr_svg(url: str) -> str:
    """导航链接 → 内联 SVG 二维码（reportlab QrCodeWidget + renderSVG，纯 Python 无新依赖，
    与 reportlab 路径 base._qr_flowable 同一二维码实现）。失败返回空串——模板退化为纯链接。"""
    if not _safe_url(url):
        return ""
    try:
        from reportlab.graphics import renderSVG
        from reportlab.graphics.barcode import qr as qr_mod
        from reportlab.graphics.shapes import Drawing
        widget = qr_mod.QrCodeWidget(url)
        bounds = widget.getBounds()
        side = max(bounds[2] - bounds[0], bounds[3] - bounds[1]) or 1.0
        d = Drawing(100, 100)
        d.add(widget)
        d.scale(100 / side, 100 / side)
        out = renderSVG.drawToString(d)  # reportlab 各版本可能返回 str 或 bytes
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        svg = out[out.index("<svg"):]    # 去掉 XML 声明/DOCTYPE，内嵌 HTML
        return svg.replace("<svg ", '<svg style="width:13mm;height:13mm;display:block" ', 1)
    except Exception:  # noqa: BLE001 — QR 失败不阻塞
        return ""


def _seg_text(seg) -> str:
    """段间连接行文案（复刻 base.route_day_block 的格式与"估算"标注）。"""
    if not seg.mode:
        return "↓ 距离未知（端点缺坐标）"
    dist = (f"{seg.distance_m / 1000:g}km" if seg.distance_m >= 1000
            else (f"{seg.distance_m}m" if seg.distance_m else ""))
    parts = [x for x in (dist, f"{seg.duration_min}min") if x]
    return f"↓ {seg.mode} " + " / ".join(parts) + ("（估算）" if seg.reference_only else "")


def _real_images(profile: TravelProfile) -> list:
    """实拍图（跳过示意/非实景/占位，与 base.day_photo 同一过滤规则）。"""
    return [i for i in profile.images
            if i.path and not any(k in (i.source or "") for k in ("示意", "非实景", "占位"))]


def _caption_map(profile: TravelProfile) -> dict:
    """景点 → 图注（简介+游玩建议；精确→包含模糊匹配，复刻 classic._caption）。"""
    note_map = {n.name: n for n in profile.spot_notes}

    def caption(spot: str) -> str:
        note = note_map.get(spot)
        if note is None:
            for name, n in note_map.items():
                if spot and (spot in name or name in spot):
                    note = n
                    break
        if note is None:
            return ""
        parts = [p for p in (f"简介：{note.intro}" if note.intro else "",
                             f"游玩：{note.activities}" if note.activities else "") if p]
        return "　".join(parts)

    return caption


def _donut_svg(items: list[tuple[str, float]], total: float) -> str:
    """预算构成环形图（SVG stroke-dasharray；插值仅数值/色值，无转义需求）。"""
    pts = [(label, float(a)) for label, a in items if isinstance(a, (int, float)) and a > 0]
    if not pts or total <= 0:
        return ""
    if len(pts) > 5:  # 超过 5 项合并为"其他"，保证环形可读
        head, rest = pts[:4], pts[4:]
        pts = head + [("其他", sum(a for _, a in rest))]
    r, c = 54.0, 70.0
    circ = 2 * math.pi * r
    segs, off = [], 0.0
    for i, (_, amount) in enumerate(pts):
        frac = min(amount / total, 1.0)
        segs.append(
            f'<circle cx="{c}" cy="{c}" r="{r}" fill="none" stroke="{PALETTE[i % len(PALETTE)]}" '
            f'stroke-width="26" stroke-dasharray="{frac * circ:.2f} {circ:.2f}" '
            f'stroke-dashoffset="{-off * circ:.2f}" transform="rotate(-90 {c} {c})"/>')
        off += frac
    center = (f'<text x="{c}" y="{c - 2}" text-anchor="middle" font-size="13" font-weight="700" '
              f'fill="#2E3766" font-family="Noto Serif SC,serif">¥{total:g}</text>'
              f'<text x="{c}" y="{c + 15}" text-anchor="middle" font-size="8" fill="#7C7A8C">'
              f'合计（全团口径）</text>')
    return f'<svg viewBox="0 0 140 140" style="width:42mm;height:42mm">{"".join(segs)}{center}</svg>'


def _stack_svg(items: list[tuple[str, float]], total: float) -> str:
    """预算构成横向堆叠条（与环形图同一份数据、同一组色）。"""
    pts = [(label, float(a)) for label, a in items if isinstance(a, (int, float)) and a > 0]
    if not pts or total <= 0:
        return ""
    w, h = 100.0, 14.0
    rects, x = [], 0.0
    for i, (_, amount) in enumerate(pts):
        frac = min(amount / total, 1.0 - x / 100.0)
        if frac <= 0:
            break
        rects.append(f'<rect x="{x:.2f}" y="0" width="{frac * 100:.2f}" height="{h}" '
                     f'fill="{PALETTE[i % len(PALETTE)]}"/>')
        x += frac * 100.0
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'style="width:100%;height:16pt;display:block;border-radius:4pt">'
            f'{"".join(rects)}</svg>')


def _temp_svg(weather_days: list[dict]) -> str:
    """逐日最高温折线（weather.days 含数值温度才渲染；日期为内部数据，仍经 escape）。"""
    pts = [(str(d.get("date") or ""), d.get("temp_max"))
           for d in weather_days if isinstance(d.get("temp_max"), (int, float))]
    if len(pts) < 2:
        return ""
    w, h, pad = 500.0, 150.0, 34.0
    temps = [t for _, t in pts]
    lo, hi = min(temps) - 3, max(temps) + 3
    span = (hi - lo) or 1.0
    step = (w - 2 * pad) / (len(pts) - 1)
    xy = [(pad + i * step, h - pad - (t - lo) / span * (h - 2 * pad)) for i, (_, t) in enumerate(pts)]
    parts = [f'<polyline points="{" ".join(f"{x:.1f},{y:.1f}" for x, y in xy)}" fill="none" '
             f'stroke="#D9A441" stroke-width="2.5"/>']
    for (x, y), (date_s, t) in zip(xy, pts):
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="#D9A441"/>')
        parts.append(f'<text x="{x:.1f}" y="{y - 8:.1f}" text-anchor="middle" font-size="10" '
                     f'fill="#B9862E">{int(t)}°</text>')
        parts.append(f'<text x="{x:.1f}" y="{h - 10:.1f}" text-anchor="middle" font-size="8.5" '
                     f'fill="#7C7A8C">{escape(date_s[5:] or date_s)}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" style="width:100%;height:auto;display:block">'
            f'{"".join(parts)}</svg>')


def build_context(profile: TravelProfile) -> dict:
    """TravelProfile → 模板上下文（封面/正文/封底共用）。"""
    basic, detail = profile.basic_info, profile.detail_info
    draft = profile.draft
    party = detail.party_size or basic.party_size or 1
    caption_of = _caption_map(profile)

    # ---- 行程总览（对齐 classic 壹章 6 行键值表）----
    d0 = basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定")
    d1 = basic.travel_dates[-1] if basic.travel_dates else ""
    dates_txt = f"{d0} ~ {d1}" if (d1 and d1 != d0) else d0
    budget_txt = f"¥{basic.budget:g}" if basic.budget else "-"
    must = "、".join(detail.must_visit[:6]) or "-"
    styles = " / ".join(basic.style or []) or "休闲"
    rhythm = {"快": "4-5 个景点/天", "慢": "1-2 个景点/天"}.get(detail.pace or "", "2-3 个景点/天")
    hotel_pref = detail.hotel.location_pref or "-"
    overview_rows = [
        ("目的地", basic.destination or "-", "出发地", basic.origin or "-"),
        ("出行时间", dates_txt, "行程天数", f"{basic.days or '-'} 天"),
        ("交通方式", basic.travel_mode or "-", "同行人数", f"{party} 人"),
        ("预算参考", f"{budget_txt}（上限 {basic.budget_max:g}）" if basic.budget_max else budget_txt,
         "旅行风格", styles),
        ("酒店偏好", hotel_pref, "必去景点", must),
        ("游览节奏", rhythm, "数据说明", "车票/酒店以订单清单为准"),
    ]
    rhythm_line = ""
    if draft:
        rhythm_line = " ｜ ".join(
            f"D{i + 1} {'→'.join(d.spots[:3]) or (d.morning or '')[:12]}"
            for i, d in enumerate(draft.days))

    # ---- 天气与穿搭（无预报整体隐藏；气温曲线需 ≥2 个数值点）----
    wdays = profile.weather.get("days") or []
    weather_rows = []
    for d in wdays[:6]:
        day_text = str(d.get("day_text", "")) or "—"
        tmin, tmax = d.get("temp_min"), d.get("temp_max")
        temp = (f"{tmin}~{tmax}℃"
                if isinstance(tmin, (int, float)) and isinstance(tmax, (int, float)) else "—")
        weather_rows.append({
            "date": str(d.get("date", "")),
            "emoji": weather_emoji(day_text),
            "day_text": day_text,
            "temp": temp,
            "advice": outfit_advice(day_text, d.get("temp_max"), d.get("temp_min")),
        })
    weather = {
        "rows": weather_rows,
        "source": profile.weather.get("source", ""),
        "curve": _temp_svg(wdays),
    } if wdays else None

    # ---- 订单清单（勾选/链接/参考值标注，对齐 classic 叁章）----
    order_rows = []
    for tk in profile.tickets:
        order_rows.append({
            "kind": "车票", "name": tk.train_no,
            "info": f"{tk.depart_time} 出发 / {tk.arrive_time} 到达，{tk.price:g} 元",
            "reason": tk.reason or "", "selected": tk.selected,
            "link": _safe_url(tk.link), "link_label": "12306 购票",
        })
    for h in profile.hotels:
        order_rows.append({
            "kind": "酒店", "name": h.name,
            "info": f"{h.price_per_night:g} 元/晚，距地标 {h.distance_km:g}km，评分 {h.rating:g}",
            "reason": h.reason or "", "selected": h.selected,
            "link": _safe_url(h.link), "link_label": "携程订房",
        })
    total_order = sum(tk.price * (2 if "往返" not in tk.train_no else 1) * party
                      for tk in profile.tickets if tk.selected)
    total_order += sum(h.price_per_night * max((basic.days or 1) - 1, 0)
                       for h in profile.hotels if h.selected)
    ref_sources = sorted({x.source for x in (*profile.tickets, *profile.hotels) if x.reference_only})
    orders = {
        "rows": order_rows,
        "total": f"已勾选订单合计（交通往返 + 住宿）：约 {total_order:g} 元",
        "ref_note": ("※ 数据来源说明：" + "；".join(ref_sources)) if ref_sources else "",
    }

    # ---- 逐日（草稿时段 + 每日路线 + 当日实拍）----
    real = _real_images(profile)
    real_by_spot = {i.spot: i for i in real}

    def match_image(spot: str):
        if spot in real_by_spot:
            return real_by_spot[spot]
        for name, item in real_by_spot.items():
            if spot and (spot in name or name in spot):
                return item
        return None

    day_ctx = []
    for i, day in enumerate((draft.days if draft else [])):
        rd = profile.routes[i] if i < len(profile.routes) else None
        stops, conns = [], []
        if rd and rd.stops:
            for j, s in enumerate(rd.stops):
                label = (f"{s.meal}·" if s.kind == "meal" and s.meal else "") + s.name
                stops.append({
                    "kind": s.kind, "badge": _STOP_BADGE.get(s.kind, "🏛"), "label": label,
                    "address": s.address or "", "note": s.note or "",
                    "nav_url": _safe_url(s.nav_url), "reference_only": s.reference_only,
                    "qr": _qr_svg(s.nav_url),
                })
                if j < len(rd.stops) - 1 and j < len(rd.segments):
                    seg = rd.segments[j]
                    if seg.mode:
                        conns.append(_seg_text(seg))
            n_meal = sum(1 for s in rd.stops if s.kind == "meal")
        else:
            n_meal = 0
        route = None
        if stops:
            names = [s["label"] for s in stops]
            meals = [s["label"] for s in stops if s["kind"] == "meal"]
            route = {
                "total_km": (rd.total_km if rd else 0.0),
                "stops": stops,
                "conns": conns,
                "summary": (rd.summary if rd else ""),
                "warnings": list(rd.warnings) if rd else [],
                "kpi": [("站点", f"{len(stops)} 个"), ("全天约", f"{(rd.total_km if rd else 0):g} km"),
                        ("餐饮", f"{n_meal} 次" if n_meal else "自理")],
                "lane_go": " → ".join(names),
                "lane_traffic": "；".join(conns) if conns else "以市内公共交通 + 步行为主（见路线表）",
                "lane_food": "、".join(meals) if meals else "当日未安排定点餐厅，随行程灵活解决",
            }
        photos = []
        for spot in day.spots:
            item = match_image(spot)
            if item:
                photos.append({"spot": item.spot, "uri": _img_uri(item.path),
                               "caption": caption_of(item.spot)})
            if len(photos) >= 3:
                break
        day_ctx.append({
            "n": i + 1, "date": day.date,
            "weekday": _weekday(day.date, basic.travel_dates),
            "spots": day.spots[:3],
            "morning": day.morning or "—", "afternoon": day.afternoon or "—",
            "evening": day.evening or "—",
            "route": route, "photos": photos,
        })

    # ---- 预算（数据源 compute_budget，对齐 classic 伍章）----
    budget_ctx = None
    if draft:
        b = compute_budget(profile, draft)
        items = [(str(r.get("item", "")), float(r.get("amount") or 0)) for r in b["items"]]
        occ = max(0.0, min(float(b["occupancy"] or 0), 1.0))
        legend = [{"label": lb, "amount": f"¥{a:g}",
                   "color": PALETTE[i % len(PALETTE)]} for i, (lb, a) in enumerate(items)]
        budget_ctx = {
            "rows": [{"item": r.get("item", ""), "note": r.get("note", ""),
                      "amount": f"{float(r.get('amount') or 0):g}"} for r in b["items"]],
            "note": (f"预算 {b['budget']:g}｜上限 {b['budget_max']:g}｜占用 {b['occupancy']:.0%}"
                     if b["occupancy"] else "—"),
            "total": f"{b['total']:g}",
            "occ_pct": f"{float(b['occupancy'] or 0):.0%}",
            "occ_bar": f"{occ * 100:.0f}%",   # 条宽按 clamp 后比例画（口径与 classic 一致）
            "occ_color": "#C4593F" if occ >= 0.9 else "#2E3766",
            "occ_disp": f"预算占用 {float(b['occupancy'] or 0):.0%}（合计 {b['total']:g} 元）",
            "warnings": list(b["warnings"]),
            "donut": _donut_svg(items, float(b["total"] or 0)),
            "stack": _stack_svg(items, float(b["total"] or 0)),
            "legend": legend,
        }

    # ---- 实景速览（对齐 classic 陆章：前 8 图 + 图注回退来源）----
    gallery = []
    for item in profile.images[:8]:
        cap = caption_of(item.spot)
        gallery.append({
            "spot": item.spot, "uri": _img_uri(item.path), "caption": cap,
            "source_note": "" if cap else (("来源：" + (item.source or "")[:93]) if item.source else ""),
        })

    # ---- 酒店（Top3 卡片，对齐 classic 柒章）----
    hotels = []
    for rank, h in enumerate(profile.hotels[:3], 1):
        hotels.append({
            "rank": rank, "name": h.name, "selected": h.selected,
            "meta": f"★ {h.rating:g}｜{h.price_per_night:g} 元/晚｜距地标 {h.distance_km:g}km",
            "desc": (f"网络评价：{h.review_digest}" if h.review_digest
                     else f"推荐理由：{h.reason or '综合评分靠前'}"),
            "img_uri": _img_uri(h.image_path),
        })

    # ---- 美食（三态回退，对齐 classic 捌章）----
    if profile.food_notes:
        foods = {"mode": "notes", "notes": [
            {"name": f.name, "intro": f.intro or "", "img_uri": _img_uri(f.image_path)}
            for f in profile.food_notes[:6]]}
    else:
        uniq: list[str] = []
        for g in profile.guide_digest:
            for f in g.foods:
                if f and f not in uniq:
                    uniq.append(f)
        if uniq[:12]:
            foods = {"mode": "grid", "grid": uniq[:12], "notes": []}
        else:
            dest = basic.destination or ""
            titles = [t for g in profile.guide_digest for t in g.raw_titles if t and dest and dest in t]
            titles = list(dict.fromkeys(titles))[:5]
            foods = {"mode": "fallback", "grid": [], "notes": [],
                     "text": ("（攻略结构化字段未返回，以下为目的地相关搜索结果标题）" + "；".join(titles))
                     if titles else "暂无（攻略通道未返回）"}

    warns: list[str] = []
    for g in profile.guide_digest:
        for w in g.warnings:
            if w and w not in warns:
                warns.append(w)
    warns = warns[:8]
    sources = [{"name": g.source_name, "url": g.source_url, "fetched": g.fetched_at}
               for g in profile.guide_digest[:3]]

    nights = max((basic.days or 1) - 1, 0)
    cover = _cover_ctx(profile, party, nights)
    return {
        "basic": {
            "origin": basic.origin or "出发地", "destination": basic.destination or "旅行",
            "days": basic.days or "-", "nights": nights, "dates_txt": dates_txt,
            "mode": basic.travel_mode or "高铁", "styles": styles,
            "party": party, "budget_txt": budget_txt,
        },
        "overview_rows": overview_rows,
        "rhythm_line": rhythm_line,
        "principles": _GENERIC_PRINCIPLES,
        "weather": weather,
        "orders": orders,
        "days": day_ctx,
        "budget": budget_ctx,
        "gallery": gallery,
        "hotels": hotels,
        "foods": foods,
        "warnings": warns,
        "sources": sources,
        "defaults": list(basic.defaults_applied),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "checklist": _GENERIC_CHECKLIST,
        "pretrip": _GENERIC_PRETRIP,
        "emergencies": _GENERIC_EMERGENCIES,
        "cover": cover,
        "back": {
            "title": "旅途愉快",
            "sub": " · ".join(basic.style or []) or "轻松出行",
            "line1": f"{basic.origin or ''} — {basic.destination or ''} · "
                     f"{basic.days or '-'} 天行程 · {party} 人同行".strip(" —·"),
            "line2": f"TripMate 多 Agent 协同旅游规划系统 · {datetime.now():%Y 年 %m 月} 统筹汇编",
        },
    }


def _cover_ctx(profile: TravelProfile, party: int, nights: int) -> dict:
    basic = profile.basic_info
    days = basic.days or "-"
    nights_txt = f"{days} 天 {nights} 晚" if nights > 0 else f"{days} 天"
    tagline = " · ".join(basic.style or []) or "轻松出行"
    chips = (basic.style or [])[:4] or ["休闲"]
    stats = [
        (nights_txt, f"{basic.travel_mode or '高铁'} 往返"),
        (basic.travel_dates[0] if basic.travel_dates else (basic.date_text or "日期待定"), "出行时间"),
        (f"{party} 人", "同行人数"),
        (f"¥{int(basic.budget)}" if basic.budget else "-", "预算参考"),
    ]
    # 封面底图：城市宣传图优先，无则取第一张实拍（示意/占位跳过，与 day_photo 同规则）
    strip_uri = ""
    for p in (profile.cover_images or []):
        strip_uri = _img_uri(p, max_w=1400)
        if strip_uri:
            break
    if not strip_uri:
        for item in _real_images(profile)[:1]:
            strip_uri = _img_uri(item.path, max_w=1400)
    return {
        "badge": f"{basic.origin or '出发地'}出发 · {tagline}",
        "title": basic.destination or "旅行",
        "subtitle": f"{nights_txt}旅行路书",
        "en_line": f"{basic.origin or ''} — {basic.destination or 'TRIP'} · TRIPMATE ITINERARY"
                   .strip(" —").upper(),
        "chips": chips,
        "stats": stats,
        "strip_uri": strip_uri,
        "foot": f"TripMate 多 Agent 系统统筹生成 · {datetime.now():%Y 年 %m 月}",
    }


def _weekday(date_str: str, travel_dates: list) -> str:
    """周几标注（复刻 base.weekday 的解析与年份推断）。"""
    from datetime import datetime as _dt
    for fmt in ("%Y-%m-%d", "%m-%d"):
        try:
            d = _dt.strptime(date_str, fmt)
            if fmt == "%m-%d" and travel_dates and travel_dates[0][:4].isdigit():
                d = d.replace(year=int(travel_dates[0][:4]))
            return "周" + "一二三四五六日"[d.weekday()]
        except ValueError:
            continue
    return ""


# ---- 通用内容（TripMate 数据模型无对应字段，按已确认决策补齐并标注"通用建议"）----

_GENERIC_PRINCIPLES = [
    ("晨", "不早起赶场", "核心景点安排在状态最好的上午，出门时间以睡饱为前提。"),
    ("居", "住宿锚点固定", "住交通枢纽附近，全程少搬行李，晚归也有熟路回酒店。"),
    ("缓", "每日留白", "每天 2-3 个核心点即可，留白比赶路更重要，累了就停。"),
    ("顺", "同区串联", "同区域景点一天串完，减少折返与无效通勤。"),
]

_GENERIC_CHECKLIST = [
    ("证件", "身份证、优惠证件（学生证/老年证）"),
    ("充电", "充电宝、充电线、插头"),
    ("雨具", "折叠伞或轻便雨衣"),
    ("防晒", "防晒霜、遮阳帽、墨镜"),
    ("药品", "常用药、创可贴、晕车药"),
    ("穿戴", "舒适的鞋（每天步行量大）"),
    ("饮水", "保温杯或随身水壶"),
    ("现金", "少量现金 + 手机支付备份"),
    ("其他", "纸巾、湿巾、垃圾袋"),
]

_GENERIC_PRETRIP = [
    "热门景点（博物馆/古迹类）多需提前 1-7 天在官方公众号或小程序实名预约，出发前逐个确认。",
    "节假日车票开售即抢，候补与备选车次同步提交；机票早买早便宜。",
    "酒店下单后确认入住时间与取消政策；旺季优先选可免费取消的房型。",
    "每天行程不宜排满：把最重要的 1-2 个点放在上午，其余视体力与天气灵活取舍。",
]

_GENERIC_EMERGENCIES = [
    ("景点约满 / 门票售罄", "改约相邻时段，或启用同区域备选景点（出发前先挑好 1-2 个平替）。"),
    ("恶劣天气", "户外景点替换为博物馆/室内街区等室内方案，雨具提前备好。"),
    ("身体不适", "调整当日节奏就近休息，必要时前往正规医院；行程留有缓冲日更从容。"),
    ("交通中断 / 停运", "改乘其他线路或网约车，先保大交通（返程），市内景点顺延取舍。"),
]
