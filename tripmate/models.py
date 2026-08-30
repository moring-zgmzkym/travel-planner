"""TravelProfile 数据骨架（企划书 §2.1/§2.2 字段表、§3.6 黑板分区、§6.3 示例）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

TRAVEL_MODES = ("高铁", "飞机", "自驾", "长途大巴")
STYLES = ("特种兵", "休闲", "亲子", "美食", "文化", "自然")
PACES = ("快", "中", "慢")


class BasicInfo(BaseModel):
    """基础信息（§2.1）。除出发地/目的地/天数外，可由系统补默认值并在草稿标注。"""

    origin: str | None = None
    destination: str | None = None
    days: int | None = None
    travel_mode: str | None = None
    travel_dates: list[str] = Field(default_factory=list)  # ["2026-10-01", "2026-10-03"]
    date_text: str | None = None                            # 原始日期表述（"十一"、"近期"）
    style: list[str] = Field(default_factory=list)
    budget: float | None = None
    budget_max: float | None = None
    party_size: int = 1
    defaults_applied: list[str] = Field(default_factory=list)  # 被默认值补齐的字段名

    def missing_required(self) -> list[str]:
        """不可默认字段缺失清单（§2.1：出发地/目的地/天数）。"""
        missing = []
        if not self.origin:
            missing.append("出发地")
        if not self.destination:
            missing.append("目的地")
        if self.days is None or self.days <= 0:
            missing.append("游玩天数")
        return missing


class HotelPref(BaseModel):
    location_pref: str | None = None
    price_range: list[float] = Field(default_factory=list)  # [300, 500]
    min_star: int | None = None


class DetailInfo(BaseModel):
    """详细信息（§2.2，整体可空）。"""

    hotel: HotelPref = Field(default_factory=HotelPref)
    must_visit: list[str] = Field(default_factory=list)
    food_restrictions: list[str] = Field(default_factory=list)
    pace: str | None = None
    party_size: int | None = None
    special_needs: str | None = None


class GuideDigestItem(BaseModel):
    """攻略摘要四元结构（§4.3），逐条标注来源与抓取时间（§2.3 可追溯性）。"""

    source_name: str
    source_url: str
    fetched_at: str
    spots: list[str] = Field(default_factory=list)
    foods: list[str] = Field(default_factory=list)
    routes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    raw_answer: str = ""          # 真实通道原始搜索摘要（mock 通道为空）
    raw_titles: list[str] = Field(default_factory=list)  # 真实通道搜索结果标题
    raw_urls: list[str] = Field(default_factory=list)
    reference_only: bool = False  # True = 模拟/摘要级参考数据（降级通道）


class TicketCandidate(BaseModel):
    train_no: str
    depart_time: str
    arrive_time: str
    duration_min: int
    price: float
    link: str
    score: float = 0.0
    selected: bool = False
    reason: str = ""
    source: str = "12306-MCP"
    reference_only: bool = False


class HotelCandidate(BaseModel):
    name: str
    price_per_night: float
    distance_km: float
    rating: float
    link: str
    score: float = 0.0
    selected: bool = False
    reason: str = ""
    source: str = "酒店 MCP"
    reference_only: bool = False
    image_path: str = ""       # 宣传图本地路径（需求 6：勾选酒店补充，失败留空）
    review_digest: str = ""    # 住客评价摘要（Tavily 检索，失败留空）


class ImageItem(BaseModel):
    spot: str
    path: str          # 本地文件路径（PDF 嵌入用）
    source: str        # 来源标注（URL 或"本地示意配图"）
    note: str = ""


class DraftDay(BaseModel):
    date: str
    morning: str = ""
    afternoon: str = ""
    evening: str = ""
    spots: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    days: list[DraftDay] = Field(default_factory=list)
    budget_items: list[dict[str, Any]] = Field(default_factory=list)
    budget_total: float = 0.0
    notes: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class DraftFeedback(BaseModel):
    confirmed: bool = False
    feedback: str = ""
    rounds_used: int = 0


class PlanInput(BaseModel):
    """信息处理 Agent 产出的统一输入包（§4.2），写入黑板 plan_input 分区。"""

    resolved: dict[str, Any] = Field(default_factory=dict)
    conflicts: list[str] = Field(default_factory=list)


class FinalDelivery(BaseModel):
    """最终交付（§3.6 final 分区）。"""

    pdf_path: str = ""
    pdf_url: str = ""
    order_summary: list[dict[str, Any]] = Field(default_factory=list)
    total_price: float = 0.0
    finished_at: str = ""


class ChangelogEntry(BaseModel):
    time: str
    version: int = 0               # 本次写入后的黑板版本号（增量重跑按版本过滤）
    writer: str                    # chatter / processor / researcher / booking / planner
    section: str                   # 黑板分区
    field: str
    old: Any = None
    new: Any = None
    reason: str = ""


class TravelProfile(BaseModel):
    """共享黑板载体（§3.6）：版本号 + updated_at，写入经 Blackboard 串行化。"""

    version: int = 0
    updated_at: str = ""
    run_id: str | None = None
    basic_info: BasicInfo = Field(default_factory=BasicInfo)
    detail_info: DetailInfo = Field(default_factory=DetailInfo)
    guide_digest: list[GuideDigestItem] = Field(default_factory=list)
    tickets: list[TicketCandidate] = Field(default_factory=list)
    hotels: list[HotelCandidate] = Field(default_factory=list)
    weather: dict[str, Any] = Field(default_factory=dict)
    images: list[ImageItem] = Field(default_factory=list)
    plan_input: PlanInput | None = None
    draft: Draft | None = None
    draft_feedback: DraftFeedback | None = None
    final: FinalDelivery | None = None
    changelog: list[ChangelogEntry] = Field(default_factory=list)


WriterName = Literal["chatter", "processor", "researcher", "booking", "planner", "system"]
