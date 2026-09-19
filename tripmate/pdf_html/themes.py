"""HTML 路书主题注册表（纯数据模块：不 import 引擎/上下文，避免循环导入）。

每个主题是一套视觉皮肤：CSS 文件、封面/封底模板、章号与章节标题文案、图表色板。
12 章的结构与数据链路全主题共用（body.html.j2 按位置索引 chapters/titles），
注册时校验两者长度一致，防止模板索引错位；主题色值取自 learning/ 样张实测
（《西安出片之旅》典雅版/行政版，pdf.js 操作符提取）。
"""

DEFAULT_THEME = "lushu"

_CHAPTERS_CN = ["壹", "贰", "叁", "肆", "伍", "陆", "柒", "捌", "玖", "拾", "拾壹", "拾贰"]
_CHAPTERS_GW = ["第一章", "第二章", "第三章", "第四章", "第五章", "第六章",
                "第七章", "第八章", "第九章", "第十章", "第十一章", "第十二章"]

_TITLES_DEFAULT = ["出发前 90 秒速览", "推荐订单清单", "抢约闹钟日历", "行程节奏与甘特",
                   "天气与穿搭提醒", "逐日行程", "预算总盘", "住宿三选一定稿",
                   "美食图鉴与注意事项", "美景图鉴", "行前48小时清单", "应急预案与 Plan B"]
# 行政公文风章节命名（对齐《西安出片之旅（行政版）》样张的公文措辞）
_TITLES_GW = ["行程总览", "购票订单清单", "行前抢约闹钟", "行程节奏与全程甘特",
              "装备与天气提醒", "逐日行程", "预算总账", "住宿方案",
              "美食版图", "出片机位图鉴", "行前准备清单", "风险预案"]

THEMES: dict[str, dict] = {
    "lushu": {
        "css": "lushu.css",
        "display_name": "出片之旅 · 红金杂志",
        "description": "实景照片满版封面 + 米白底红金杂志排版",
        "cover": "cover.html.j2",
        "back": "back.html.j2",
        "chapter_labels": _CHAPTERS_CN,
        "titles": _TITLES_DEFAULT,
        "palette": ["#B03A2E", "#D9A441", "#2E3766", "#5F7E62", "#C7803C", "#8A94B0"],
        "gantt_colors": ("#D9A441", "#2E3766", "#B03A2E"),   # 上午/下午/晚上
        "svg_ink": "#2E3766",        # 环形图中心字
        "svg_accent": "#D9A441",     # 温度折线
        "svg_accent_text": "#B9862E",
        "svg_muted": "#7C7A8C",
        "occ_ok": "#2E3766",
        "occ_warn": "#C4593F",
        "page_num": (124 / 255, 122 / 255, 140 / 255),
        "page_num_last": (138 / 255, 148 / 255, 176 / 255),
    },
    "elegant": {
        "css": "elegant.css",
        "display_name": "典雅墨卷",
        "description": "宣纸米底 + 墨色宋体 + 朱砂印章的文人雅致排版",
        "cover": "cover_elegant.html.j2",
        "back": "back_elegant.html.j2",
        "chapter_labels": _CHAPTERS_CN,
        "titles": _TITLES_DEFAULT,
        "palette": ["#9C3B2C", "#A89B7E", "#57503F", "#4F6478", "#8F8674", "#C9BC9F"],
        "gantt_colors": ("#C9BC9F", "#57503F", "#9C3B2C"),
        "svg_ink": "#453E32",
        "svg_accent": "#A89B7E",
        "svg_accent_text": "#7A6A4C",
        "svg_muted": "#8F8674",
        "occ_ok": "#57503F",
        "occ_warn": "#9C3B2C",
        "page_num": (107 / 255, 99 / 255, 83 / 255),
        "page_num_last": (143 / 255, 134 / 255, 116 / 255),
    },
    "executive": {
        "css": "executive.css",
        "display_name": "行政公文",
        "description": "白底藏蓝 + 公文编号 + 规范表格的行政排版",
        "cover": "cover_executive.html.j2",
        "back": "back_executive.html.j2",
        "chapter_labels": _CHAPTERS_GW,
        "titles": _TITLES_GW,
        "palette": ["#1F3A5F", "#3D5A80", "#8A93A0", "#B4C0CE", "#56687A", "#C9D2DD"],
        "gantt_colors": ("#B4C0CE", "#1F3A5F", "#454C57"),
        "svg_ink": "#1F3A5F",
        "svg_accent": "#3D5A80",
        "svg_accent_text": "#56687A",
        "svg_muted": "#8A93A0",
        "occ_ok": "#1F3A5F",
        "occ_warn": "#B03A2E",
        "page_num": (138 / 255, 147 / 255, 160 / 255),
        "page_num_last": (107 / 255, 117 / 255, 130 / 255),
    },
}


def _validate() -> None:
    n_sections = 12
    for name, cfg in THEMES.items():
        assert len(cfg["chapter_labels"]) == n_sections, f"主题 {name} 章号数 ≠ {n_sections}"
        assert len(cfg["titles"]) == n_sections, f"主题 {name} 章节数 ≠ {n_sections}"
        assert len(cfg["gantt_colors"]) == 3, f"主题 {name} 甘特色数 ≠ 3"


_validate()


def get_theme(name: str | None) -> dict:
    """按名取主题配置；未知名抛 ValueError（语义对齐 pdf_templates.get_template）。"""
    name = name or DEFAULT_THEME
    if name not in THEMES:
        raise ValueError(f"未知 PDF 主题 '{name}'，可选：{'、'.join(sorted(THEMES))}")
    return THEMES[name]


def resolve_theme(name: str | None) -> str:
    """None/未知值（含旧会话残留的 "cartoon" 等）一律回退默认主题名。
    返回主题名字符串，供渲染链路与元数据使用。"""
    if isinstance(name, str) and name in THEMES:
        return name
    return DEFAULT_THEME


def list_themes() -> list[dict]:
    """主题元数据（/api/templates 与前端选择器用）。"""
    return [{"name": k, "display_name": c["display_name"], "description": c["description"]}
            for k, c in THEMES.items()]
