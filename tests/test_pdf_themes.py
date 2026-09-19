"""PDF 主题系统测试：注册表语义 / 三主题渲染冒烟 / 行政主题标题与书签 / build_pdf template 生效。

引擎（Playwright Chromium / 系统 Edge）不可用时渲染类测试整模块 skip
（与 test_pdf_html 同策略，生产路径自动降级 reportlab cartoon）。"""

import pytest

from test_pdf import _profile_bb, _routes_profile_bb  # tests 目录在 sys.path

from test_pdf_html import html_engine  # noqa: F401 — 复用引擎可用性探测 fixture


def test_registry_semantics():
    from tripmate.pdf_html.themes import THEMES, get_theme, list_themes, resolve_theme

    assert resolve_theme(None) == "lushu"
    assert resolve_theme("cartoon") == "lushu", "旧会话残留的 reportlab 模板名回退默认主题"
    assert resolve_theme("executive") == "executive"
    assert resolve_theme("no_such") == "lushu"
    with pytest.raises(ValueError):
        get_theme("no_such_theme")
    names = {t["name"] for t in list_themes()}
    assert names == {"lushu", "elegant", "executive"}
    for cfg in THEMES.values():
        assert len(cfg["chapter_labels"]) == 12 and len(cfg["titles"]) == 12


def test_api_templates_endpoint():
    """/api/templates：html 主题（engine=html）+ reportlab 注册表（engine=reportlab）合并返回。"""
    import asyncio

    import httpx
    from tripmate.gateway.app import app

    async def main():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t") as c:
            r = await c.get("/api/templates")
            assert r.status_code == 200
            return r.json()["templates"]

    items = asyncio.run(main())
    html = [t for t in items if t.get("engine") == "html"]
    rl = [t for t in items if t.get("engine") == "reportlab"]
    assert {t["name"] for t in html} == {"lushu", "elegant", "executive"}
    assert any(t["name"] == "cartoon" for t in rl), "reportlab 降级引擎仍在列表中"
    assert all("display_name" in t and "description" in t for t in items)


def _render(theme, run_id):
    from tripmate.pdf_html import render
    path = render(_routes_profile_bb().profile, run_id=run_id, theme=theme)
    import pymupdf
    return path, pymupdf.open(path)


@pytest.mark.parametrize("theme", ["lushu", "elegant", "executive"])
def test_render_theme_smoke(html_engine, theme):
    import pymupdf
    from tripmate.pdf_html.themes import get_theme
    from pathlib import Path

    path, doc = _render(theme, f"thm_{theme}")
    try:
        assert Path(path).read_bytes()[:4] == b"%PDF"
        assert doc.page_count >= 3, "至少封面+正文+封底"
        text = " ".join(doc[i].get_text() for i in range(doc.page_count))
        cfg = get_theme(theme)
        # 每个主题：第 1/7 章标题按主题文案渲染；页码含总页数
        assert cfg["titles"][0] in text and cfg["titles"][6] in text
        assert f"{doc.page_count} / {doc.page_count}" in text
        # 高德导航直达链接存在（删二维码后导航功能不丢，全主题一致）
        links = [lk for i in range(doc.page_count) for lk in doc[i].get_links()
                 if lk.get("kind") == pymupdf.LINK_URI]
        assert any("uri.amap.com" in (lk.get("uri") or "") for lk in links)
        # 书签：第 1 章定位到第一个正文页（页 2）
        toc = dict((t[1], t[2]) for t in doc.get_toc())
        assert toc.get(cfg["titles"][0]) == 2, "第 1 章书签定位到首个正文页"
    finally:
        doc.close()
        Path(path).unlink(missing_ok=True)


def test_executive_theme_labels(html_engine):
    """行政公文主题：章号"第一章"与公文命名章节标题（预算总账）实际渲染。"""
    from pathlib import Path

    path, doc = _render("executive", "thm_gw01")
    try:
        text = " ".join(doc[i].get_text() for i in range(doc.page_count))
        assert "第一章" in text and "预算总账" in text and "行程总览" in text
        toc = dict((t[1], t[2]) for t in doc.get_toc())
        assert "预算总账" in toc and "行程总览" in toc, "行政命名章节进入书签"
    finally:
        doc.close()
        Path(path).unlink(missing_ok=True)


def test_build_pdf_template_effective(html_engine):
    """build_pdf 的 template 参数真正生效：executive 出行政版式；未知值回退默认主题。"""
    from tripmate.pdf_gen import build_pdf
    from pathlib import Path
    import pymupdf

    path = build_pdf(_profile_bb().profile, run_id="thmbp01", template="executive")
    try:
        doc = pymupdf.open(path)
        text = " ".join(doc[i].get_text() for i in range(doc.page_count))
        assert "第一章" in text and "预算总账" in text
        doc.close()
    finally:
        Path(path).unlink(missing_ok=True)

    path = build_pdf(_profile_bb().profile, run_id="thmbp02", template="cartoon")
    try:
        doc = pymupdf.open(path)
        text = " ".join(doc[i].get_text() for i in range(doc.page_count))
        assert "出发前 90 秒速览" in text, "未知模板名回退默认主题（html 出片之旅）"
        doc.close()
    finally:
        Path(path).unlink(missing_ok=True)
