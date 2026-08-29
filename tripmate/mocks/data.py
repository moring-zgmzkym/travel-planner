"""降级用模拟知识库（§7 降级方案）：数据标注「参考值」，来源标注模拟通道。

覆盖验收用例（§10.1：上海→成都 3 天）所需数据，其余城市/线路按规则生成合理参考值。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

# ---- 城市知识库（攻略摘要 + 门票价 + 坐标） ----
CITY_KB: dict[str, dict] = {
    "成都": {
        "spots": ["大熊猫繁育研究基地", "宽窄巷子", "锦里古街", "武侯祠", "杜甫草堂", "人民公园", "春熙路太古里", "都江堰", "青城山", "东郊记忆"],
        "foods": ["火锅", "串串香", "冒菜", "龙抄手", "钟水饺", "蛋烘糕", "钵钵鸡", "甜水面", "兔头", "冰粉"],
        "routes": ["D1 熊猫基地(上午)→宽窄巷子(下午)→锦里夜景(晚上)", "D2 武侯祠→杜甫草堂→人民公园鹤鸣茶社→春熙路太古里夜景", "D3 都江堰一日往返→晚上火锅"],
        "warnings": ["熊猫基地一定要早上开园就去，下午熊猫基本在睡觉", "锦里和宽窄巷子小吃溢价高，浅尝即可", "春熙路商圈打车难，优先地铁 2/3 号线", "都江堰往返建议城际铁路犀浦站上车"],
        "tickets": {"大熊猫繁育研究基地": 55, "都江堰": 80, "青城山": 80, "武侯祠": 50, "杜甫草堂": 50, "东郊记忆": 0, "宽窄巷子": 0, "锦里古街": 0, "人民公园": 0, "春熙路太古里": 0},
        "landmark": "春熙路",
        "food_cost_per_day": 150,
    },
    "上海": {"landmark": "人民广场", "food_cost_per_day": 160},
    "北京": {
        "spots": ["故宫博物院", "天安门广场", "颐和园", "天坛", "南锣鼓巷", "八达岭长城", "什刹海", "国家博物馆"],
        "foods": ["北京烤鸭", "炸酱面", "豆汁儿", "卤煮火烧", "铜锅涮肉"],
        "routes": ["D1 天安门→故宫→景山→什刹海", "D2 颐和园→圆明园→五道口", "D3 八达岭长城→鸟巢水立方夜景"],
        "warnings": ["故宫需提前 7 天实名预约", "长城建议乘 S2 线或专线巴士", "豆汁儿慎点，多数人喝不惯"],
        "tickets": {"故宫博物院": 60, "颐和园": 30, "天坛": 15, "八达岭长城": 40},
        "landmark": "天安门", "food_cost_per_day": 180,
    },
    "西安": {
        "spots": ["秦始皇兵马俑", "西安城墙", "大雁塔", "回民街", "陕西历史博物馆", "华清宫", "钟鼓楼"],
        "foods": ["肉夹馍", "羊肉泡馍", "凉皮", "biangbiang面", "甑糕"],
        "routes": ["D1 城墙骑行→钟鼓楼→回民街", "D2 兵马俑→华清宫", "D3 陕历博→大雁塔→大唐不夜城"],
        "warnings": ["兵马俑请官方渠道购票，火车站黑车多", "陕历博免费但需预约", "回民街主街贵，往西羊市走"],
        "tickets": {"秦始皇兵马俑": 120, "西安城墙": 54, "华清宫": 120, "大雁塔": 40},
        "landmark": "钟楼", "food_cost_per_day": 130,
    },
    "重庆": {
        "spots": ["洪崖洞", "磁器口古镇", "长江索道", "解放碑", "李子坝轻轨穿楼", "南山一棵树"],
        "foods": ["九宫格火锅", "重庆小面", "酸辣粉", "毛血旺", "山城汤圆"],
        "routes": ["D1 解放碑→长江索道→南山一棵树夜景", "D2 磁器口→李子坝→鹅岭二厂", "D3 洪崖洞→朝天门两江游"],
        "warnings": ["洪崖洞夜景人多，错峰 22 点后", "导航在重庆经常失灵，跟着 locals 走", "火锅默认重辣，点微辣都要慎重"],
        "tickets": {"长江索道": 30, "南山一棵树": 30},
        "landmark": "解放碑", "food_cost_per_day": 140,
    },
}

# 通用兜底（未收录城市）：按城市名生成模板化参考数据
GENERIC_TEMPLATE = {
    "spots": ["市中心历史街区", "城市中央公园", "当地博物馆", "标志性观景台", "近郊自然景区"],
    "foods": ["当地特色小吃街美食", "老字号招牌菜", "夜市烧烤"],
    "routes": ["D1 市中心地标游览", "D2 文化场馆+特色街区", "D3 近郊自然风光"],
    "warnings": ["旺季景点请提前线上购票", "市区高峰期拥堵，优先公共交通"],
    "tickets": {},
    "landmark": "市中心",
    "food_cost_per_day": 120,
}


def kb_for_city(city: str) -> dict:
    return CITY_KB.get(city, {**GENERIC_TEMPLATE, "spots": [f"{city}{s}" for s in GENERIC_TEMPLATE["spots"]]})


def spot_ticket_price(spot: str) -> float:
    """门票参考价：先查知识库，未收录给默认 50 元并标注参考。"""
    for kb in CITY_KB.values():
        if spot in kb.get("tickets", {}):
            return kb["tickets"][spot]
    return 50.0


# ---- 攻略摘要（模拟三路搜索结果，reference_only=True） ----
def mock_guide_digest(city: str, month_hint: str = "") -> list[dict]:
    kb = kb_for_city(city)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    kw = f"{city}旅游攻略" + (f" {month_hint}" if month_hint else "")
    return [
        {
            "source_name": "小红书（搜索摘要级）",
            "source_url": f"https://www.xiaohongshu.com/search_result?keyword={city}攻略",
            "fetched_at": ts,
            "spots": kb["spots"][:6],
            "foods": kb["foods"][:5],
            "routes": kb["routes"][:1],
            "warnings": [kb["warnings"][0]] if kb["warnings"] else [],
            "reference_only": True,
        },
        {
            "source_name": "马蜂窝（搜索摘要级）",
            "source_url": f"https://www.mafengwo.cn/search/q.php?q={kw}",
            "fetched_at": ts,
            "spots": kb["spots"][3:9],
            "foods": kb["foods"][2:7],
            "routes": kb["routes"][1:2],
            "warnings": kb["warnings"][1:2],
            "reference_only": True,
        },
        {
            "source_name": "百度搜索（公开网页摘要）",
            "source_url": f"https://www.baidu.com/s?wd={kw}",
            "fetched_at": ts,
            "spots": kb["spots"][-4:],
            "foods": kb["foods"][-3:],
            "routes": kb["routes"][-1:],
            "warnings": kb["warnings"][-1:],
            "reference_only": True,
        },
    ]


# ---- 车票（模拟班次表；上海→成都 用验收用例的参考班次） ----
def mock_train_tickets(origin: str, destination: str, date: str) -> list[dict]:
    key = f"{origin}-{destination}"
    if key == "上海-成都":
        rows = [
            ("G1974", "06:58", "19:26", 748, 926.5),
            ("G1976", "07:42", "20:31", 769, 926.5),
            ("G1476", "08:12", "21:05", 773, 897.5),
            ("D636", "09:15", "22:40", 805, 609.0),
            ("G2186", "10:06", "22:58", 772, 926.5),
        ]
    else:
        rows = _gen_trains(origin, destination)
    return [
        {
            "train_no": t, "depart_time": d, "arrive_time": a, "duration_min": dur, "price": p,
            "link": f"https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc&fs={origin}&ts={destination}&date={date}",
            "source": "模拟班次表（12306-MCP 不可用，降级参考值）",
            "reference_only": True,
        }
        for (t, d, a, dur, p) in rows
    ]


def _gen_trains(origin: str, destination: str) -> list[tuple]:
    """任意城市对：按名称哈希生成稳定合理的 5 个高铁班次。"""
    seed = int(hashlib.md5(f"{origin}->{destination}".encode()).hexdigest(), 16)
    rows = []
    for i in range(5):
        h = (seed >> (i * 6)) & 0xFFFF
        dep_min = 6 * 60 + 40 + (h % 240)          # 06:40 ~ 10:40 出发
        dur = 300 + ((h >> 8) % 480)               # 5h ~ 13h
        price = round(300 + (h % 600) + 0.5, 1)    # 300 ~ 900
        rows.append((
            f"G{1800 + (h % 400)}",
            f"{dep_min // 60:02d}:{dep_min % 60:02d}",
            f"{(dep_min + dur) // 60 % 24:02d}:{(dep_min + dur) % 60:02d}",
            dur, price,
        ))
    return rows


def mock_flight_tickets(origin: str, destination: str, date: str) -> list[dict]:
    """飞机出行：无机票 MCP，按 §7 常态走搜索摘要级参考数据。"""
    seed = int(hashlib.md5(f"{origin}=>{destination}".encode()).hexdigest(), 16)
    rows = []
    for i in range(4):
        h = (seed >> (i * 5)) & 0xFFF
        dep = 7 * 60 + (h % 300)
        rows.append({
            "train_no": f"MU{5000 + (h % 900)}",
            "depart_time": f"{dep // 60:02d}:{dep % 60:02d}",
            "arrive_time": f"{(dep + 150 + h % 60) // 60 % 24:02d}:{(dep + 150 + h % 60) % 60:02d}",
            "duration_min": 150 + h % 60,
            "price": round(700 + (h % 900), 0),
            "link": f"https://flights.ctrip.com/online/list/oneway-{origin}-{destination}?depdate={date}",
            "source": "搜索摘要级参考数据（无机票 MCP，§7 降级）",
            "reference_only": True,
        })
    return rows


def mock_transport_estimate_km(origin: str, destination: str) -> int:
    """自驾/大巴：往返里程估算（km，单程）。"""
    return 800 + int(hashlib.md5(f"{origin}~{destination}".encode()).hexdigest(), 16) % 1500


# ---- 酒店（模拟候选，春熙路用验收用例数据） ----
def mock_hotels(city: str, landmark: str | None) -> list[dict]:
    if city == "成都" and (not landmark or "春熙" in landmark):
        rows = [
            ("全季酒店（成都春熙路店）", 429, 0.4, 4.7),
            ("亚朵酒店（成都天府广场店）", 488, 0.8, 4.8),
            ("如家精选（春熙路地铁站店）", 319, 0.6, 4.4),
            ("桔子酒店（成都太古里店）", 396, 1.1, 4.6),
            ("丽枫酒店（成都春熙路店）", 358, 0.9, 4.5),
        ]
    else:
        kb = kb_for_city(city)
        lm = landmark or kb["landmark"]
        rows = [
            (f"全季酒店（{city}{lm}店）", 320 + i * 37, round(0.3 + i * 0.45, 1), round(4.3 + i * 0.1, 1))
            for i in range(5)
        ]
    return [
        {
            "name": n, "price_per_night": p, "distance_km": d, "rating": r,
            "link": f"https://hotels.ctrip.com/hotels/list?city={city}&keyword={n.split('（')[0]}",
            "source": "模拟酒店库（酒店 MCP 不可用，降级参考值）",
            "reference_only": True,
        }
        for (n, p, d, r) in rows
    ]


# ---- 天气（降级参考） ----
def mock_weather(dates: list[str], city: str) -> dict:
    conds = ["晴", "多云", "阴", "小雨", "多云转晴"]
    seed = int(hashlib.md5(city.encode()).hexdigest(), 16)
    days = []
    for i, d in enumerate(dates):
        days.append({
            "date": d,
            "day_text": conds[(seed + i) % len(conds)],
            "temp_min": 14 + (seed + i) % 8,
            "temp_max": 22 + (seed + i) % 10,
        })
    return {"city": city, "source": "模拟天气（天气服务不可用，降级参考值）", "reference_only": True, "days": days}


# ---- 日期工具 ----
def expand_dates(start: str, days: int) -> list[str]:
    d0 = datetime.strptime(start, "%Y-%m-%d")
    return [(d0 + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
