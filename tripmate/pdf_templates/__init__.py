"""PDF 模板注册表（降级引擎）：reportlab 侧仅保留 cartoon；主渲染路径见 pdf_html。

新增模板在此登记即可被 render_reportlab(template=...) 选用（build_pdf 主路径固定
HTML，未知名一律回退默认模板，不再抛 ValueError）。
"""

from __future__ import annotations

from .base import BaseTripTemplate
from .cartoon import CartoonTemplate

_TEMPLATE_CLASSES: list[type[BaseTripTemplate]] = [
    CartoonTemplate,
]

REGISTRY: dict[str, BaseTripTemplate] = {cls.name: cls() for cls in _TEMPLATE_CLASSES}
DEFAULT_TEMPLATE = "cartoon"


def get_template(name: str | None = None) -> BaseTripTemplate:
    name = name or DEFAULT_TEMPLATE
    if name not in REGISTRY:
        raise ValueError(f"未知 PDF 模板 '{name}'，可选：{'、'.join(sorted(REGISTRY))}")
    return REGISTRY[name]


def list_templates() -> list[dict]:
    """模板元数据（供降级引擎说明与脚本验证）。"""
    return [{"name": t.name, "display_name": t.display_name,
             "description": t.description, "scenes": t.scenes}
            for t in REGISTRY.values()]
