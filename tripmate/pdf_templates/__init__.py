"""PDF 模板注册表：新增模板在此登记即可被 build_pdf(template=...) 选用。"""

from __future__ import annotations

from .base import BaseTripTemplate
from .card import CardTemplate
from .classic import ClassicTemplate
from .guide import GuideTemplate
from .journal import JournalTemplate
from .minimal import MinimalTemplate
from .warm import WarmTemplate

_TEMPLATE_CLASSES: list[type[BaseTripTemplate]] = [
    ClassicTemplate,
    GuideTemplate,
    MinimalTemplate,
    CardTemplate,
    WarmTemplate,
    JournalTemplate,
]

REGISTRY: dict[str, BaseTripTemplate] = {cls.name: cls() for cls in _TEMPLATE_CLASSES}
DEFAULT_TEMPLATE = "classic"


def get_template(name: str | None = None) -> BaseTripTemplate:
    name = name or DEFAULT_TEMPLATE
    if name not in REGISTRY:
        raise ValueError(f"未知 PDF 模板 '{name}'，可选：{'、'.join(sorted(REGISTRY))}")
    return REGISTRY[name]


def list_templates() -> list[dict]:
    """模板元数据（供前端下拉选择与 Agent 推荐）。"""
    return [{"name": t.name, "display_name": t.display_name,
             "description": t.description, "scenes": t.scenes}
            for t in REGISTRY.values()]
