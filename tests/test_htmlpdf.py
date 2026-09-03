"""方向二工具链单测（docs/html-designer-plan.md 任务 1.4 / 4.1）：消毒 / 套壳 / 渲染 / 诊断。

渲染类测试在无任何可用内核（Playwright chromium 与 Edge 均缺）时跳过。
"""

from pathlib import Path

import pytest

from tripmate.design import load_print_css
from tripmate.designer import parse_theme_line
from tripmate.tools.htmlpdf import (HtmlTooLargeError, inspect_pdf, render_html_pdf,
                                    sanitize_html, wrap_html)

try:  # 渲染可用性探测（chromium 版本漂移兜底 / Edge CLI）
    from tripmate.tools.htmlpdf import _discover_chromium, _find_edge
    _RENDER_AVAILABLE = bool(_discover_chromium() or _find_edge())
except Exception:  # noqa: BLE001
    _RENDER_AVAILABLE = False

RENDER_SKIP = pytest.mark.skipif(not _RENDER_AVAILABLE, reason="本机无 Chromium/Edge 内核")


# ---------- 消毒器（D2 允许列表）----------

def test_sanitize_strips_script_and_event_handlers():
    frag = '<div onclick="steal()">正常</div><script>alert(1)</script><p style="color:red">文本</p>'
    r = sanitize_html(frag)
    assert "<script" not in r.html and "alert" not in r.html
    assert "onclick" not in r.html
    assert "正常" in r.html and 'style="color:red"' in r.html
    assert not r.clean  # 有拦截动作要记 findings


def test_sanitize_strips_meta_base_iframe_and_nested_script():
    frag = ('<iframe src="https://evil"></iframe><base href="https://evil.com/">'
            '<meta http-equiv="refresh" content="0;url=file:///C:/x">'
            "<scr<script></script>ipt>alert(2)</script><b>保留</b>")
    r = sanitize_html(frag)
    assert "iframe" not in r.html and "base" not in r.html and "meta" not in r.html
    # <scr<script> 分割在 void 标签不再吞文档后现形为无害文本（浏览器同样按未知元素+
    # 文本处理，无 script 执行面）：断言无 script 标签残留，而非文本不可见
    assert "<script" not in r.html.lower() and "evil" not in r.html
    assert "保留" in r.html


def test_sanitize_strips_external_img_and_css_import():
    frag = ('<img src="https://cdn.example.com/x.jpg" srcset="a 2x">'
            '<style>@import url(https://evil.com/f.css);'
            'body{background:url("https://evil.com/t.png")}p{color:#333}</style>')
    r = sanitize_html(frag)
    assert "img" not in r.html and "srcset" not in r.html
    assert "@import" not in r.html and "evil.com" not in r.html
    assert "color:#333" in r.html  # 合规样式保留


def test_sanitize_file_uri_whitelist_and_unc_rejection():
    from tripmate.config import IMAGE_DIR
    inside = (IMAGE_DIR / "x.jpg").resolve().as_uri()
    outside = Path("C:/Users/someone/secret.png").resolve().as_uri()
    unc = "file://server/share/a.jpg"
    assert "img" in sanitize_html(f'<img src="{inside}">').html
    assert "img" not in sanitize_html(f'<img src="{outside}">').html
    assert "img" not in sanitize_html(f'<img src="{unc}">').html
    # 手工拼的反斜杠绝对路径（非法 URI）也必须剥除
    assert "img" not in sanitize_html('<img src="file:///D:\\其他\\路径.png">').html


def test_sanitize_a_href_rules():
    keep = sanitize_html('<a href="https://kyfw.12306.cn/x">订单</a>').html
    assert 'href="https://kyfw.12306.cn/x"' in keep
    stripped = sanitize_html('<a href="javascript:alert(1)">点我</a>').html
    assert "javascript" not in stripped and "点我" in stripped


def test_sanitize_data_uri_type_and_size_cap():
    ok_png = "data:image/png;base64," + "A" * 100
    bad_svg = "data:image/svg+xml;base64," + "A" * 100
    assert "img" in sanitize_html(f'<img src="{ok_png}">').html
    assert "img" not in sanitize_html(f'<img src="{bad_svg}">').html
    # 超限 data URI：片段 512KB 上限先于单图 2MB 上限触发（双重防线中片段闸更严）
    huge = "data:image/png;base64," + "A" * (600 * 1024)
    with pytest.raises(HtmlTooLargeError):
        sanitize_html(f'<img src="{huge}">')


def test_sanitize_html_too_large():
    with pytest.raises(HtmlTooLargeError):
        sanitize_html("<p>" + "x" * (513 * 1024) + "</p>")


def test_sanitize_svg_internal_refs_only():
    ok = sanitize_html('<svg><use href="#icon"/><path d="M0 0"/></svg>').html
    assert 'href="#icon"' in ok
    bad = sanitize_html('<svg><use href="https://evil.com/i.svg"/></svg>').html
    assert "evil.com" not in bad


# ---------- 套壳（D2 输出契约）----------

def test_wrap_html_injects_charset_css_theme_footer():
    wrapped = wrap_html('<section class="cover">x</section>', title="行程计划_成都", theme="theme-warm")
    assert wrapped.startswith("<!DOCTYPE html>")
    assert '<meta charset="utf-8">' in wrapped
    assert 'class="theme-warm"' in wrapped
    assert "Microsoft YaHei" in wrapped  # print.css 已内联
    assert "@page" in wrapped
    assert "AI 版面设计师生成" in wrapped  # 来源页脚
    assert "</html>" in wrapped


def test_wrap_html_unknown_theme_falls_back():
    wrapped = wrap_html("<p>x</p>", title="t", theme="theme-hack")
    assert 'class="theme-azure"' in wrapped


def test_wrap_html_escapes_title_injection():
    """title 来自用户画像字段，必须实体转义（评审 #3）。"""
    evil = '</title><img src=x onerror=alert(1)>'
    wrapped = wrap_html("<p>x</p>", title=f"行程计划_{evil}")
    assert wrapped.count("<title>") == 1 and wrapped.count("</title>") == 1
    assert "<img src=x" not in wrapped  # 未转义时这里会是一个真标签
    assert "&lt;/title&gt;" in wrapped  # 已实体转义


def test_sanitize_css_escape_bypass_blocked():
    """CSS 十六进制转义绕过 @import 剥除（评审 #2）：解码后再匹配。"""
    r = sanitize_html('<style>@\\69 mport "https://evil.com/x.css";p{color:red}</style>')
    assert "@import" not in r.html.lower() and "evil.com" not in r.html
    assert "color:red" in r.html


def test_sanitize_svg_title_kept():
    """SVG <title> 是无障碍描述，不应整节点丢弃（评审 #7）。"""
    r = sanitize_html('<svg><title>预算环图</title><circle r="5"/></svg>')
    assert "预算环图" in r.html


def test_sanitize_unclosed_style_dropped_silently():
    """未闭合 <style>：内容静默丢弃、不崩溃、不泄漏原文。"""
    r = sanitize_html("<p>正文</p><style>body{color:red}")
    assert "正文" in r.html and "color:red" not in r.html


def test_sanitize_attr_roundtrip():
    """属性值 & 与 " 双转义，输出为不动点（评审 #11）。"""
    r = sanitize_html('<div title="a&b&quot;c">x</div>')
    assert 'title="a&amp;b&quot;c"' in r.html


def test_parse_theme_line():
    theme, rest = parse_theme_line("THEME: theme-mono\n<p>正文</p>")
    assert theme == "theme-mono" and rest == "<p>正文</p>"
    theme2, rest2 = parse_theme_line("<p>无声明</p>")
    assert theme2 == "theme-azure" and "无声明" in rest2
    # 非法主题名：声明行同样剥掉，不泄漏为正文可见文本（评审 #8）
    theme3, rest3 = parse_theme_line("THEME: theme-bogus\n<p>正文</p>")
    assert theme3 == "theme-azure" and "THEME" not in rest3 and "<p>正文</p>" in rest3


# ---------- 渲染 + 诊断（D3）----------

@RENDER_SKIP
def test_render_and_inspect_end_to_end(tmp_path):
    frag = ('<section class="cover"><h1 class="cover-title">测试行程</h1></section>'
            '<section class="sec"><h2>行程总览</h2><p>三日行程安排。</p></section>'
            '<section class="sec"><h2>预算一览</h2><p>合计 ¥100。</p></section>'
            '<section class="sec"><h2>推荐订单</h2><p>车票订单。</p></section>')
    wrapped = wrap_html(frag, title="测试行程")
    html_path = tmp_path / "trip_test.html"
    html_path.write_text(wrapped, encoding="utf-8")
    pdf_path = tmp_path / "trip_test.pdf"
    report = render_html_pdf(html_path, pdf_path)
    assert report["ok"], report["error"]
    diag = inspect_pdf(pdf_path)
    assert diag["ok"], diag
    assert diag["pages"] >= 1
    assert diag["missing_keywords"] == []


@RENDER_SKIP
def test_render_invalid_input_reports_failure(tmp_path):
    html_path = tmp_path / "bad.html"
    html_path.write_text("<p>缺关键词的普通文本</p>", encoding="utf-8")
    pdf_path = tmp_path / "bad.pdf"
    report = render_html_pdf(html_path, pdf_path)
    if report["ok"]:  # 渲染成功但关键词缺失 → 诊断判失败
        diag = inspect_pdf(pdf_path)
        assert not diag["ok"] and diag["missing_keywords"]
    else:
        assert report["error"]


def test_inspect_missing_pdf():
    diag = inspect_pdf(Path("Z:/不存在/no.pdf"))
    assert not diag["ok"]


def test_print_css_loadable():
    css = load_print_css()
    assert "@page" in css and "break-inside" in css and "theme-mono" in css


def test_print_css_locks_d1_promises():
    """D1 承诺回归锁：A4 版式/中文字体栈/4 套主题，改丢即红。"""
    css = load_print_css()
    assert "size" in css and "A4" in css
    assert "Microsoft YaHei" in css and "DengXian" in css and "SimSun" in css
    for theme in ("theme-azure", "theme-warm", "theme-fresh", "theme-mono"):
        assert theme in css


def test_golden_sample_size_locked():
    """金样体积回归锁：3–5K token 目标，超限即提示词隐性成本失控。"""
    from tripmate.design import load_golden_sample
    sample = load_golden_sample()
    assert len(sample) < 10000  # 当前约 7.7K 字符，留余量但禁无声膨胀


def test_sanitize_css_case_and_comment_bypass_blocked():
    """CSS 大小写/注释分割绕过（复核 CONFIRMED）：外链一律剥除且有 finding。"""
    r = sanitize_html('<style>p{background:URL("https://evil.com/t.png");color:red}</style>')
    assert "evil.com" not in r.html and "color:red" in r.html
    r2 = sanitize_html('<style>p{background:u/**/rl("https://evil.com/t.png")}</style>')
    assert "evil.com" not in r2.html
    r3 = sanitize_html('<style>@/**/import "https://evil.com/x.css";p{color:red}</style>')
    assert "evil.com" not in r3.html.lower() and "color:red" in r3.html


def test_sanitize_unterminated_import_dropped_fail_closed():
    """EOF 结尾的 @import（合法 CSS）：剥除失败即整段丢弃，不 fail-open。"""
    r = sanitize_html('<style>@import "https://evil.com/x.css"</style>')
    assert "evil.com" not in r.html.lower()


def test_sanitize_void_drop_tags_dont_swallow_document():
    """void 型 drop 标签（meta/link/base）不得吞掉后续全文。"""
    r = sanitize_html('<meta charset="x"><p>hello</p>')
    assert "hello" in r.html
    r2 = sanitize_html('<link rel="stylesheet" href="https://evil.com/x.css"><p>world</p>')
    assert "world" in r2.html and "evil.com" not in r2.html


def test_sanitize_svg_presentation_url_rules():
    """SVG fill/stroke：外部 url() 剥除，内部 url(#...) 保留。"""
    bad = sanitize_html('<svg><circle r="5" fill="url(https://evil.com/g)"/></svg>').html
    assert "evil.com" not in bad
    ok = sanitize_html('<svg><circle r="5" fill="url(#grad)"/></svg>').html
    assert "url(#grad)" in ok


def test_sanitize_self_closed_nonvoid_emits_open_tag():
    """<div/> 按 HTML 词法是开始标签：输出开始标签，不凭空自闭合。"""
    r = sanitize_html("<div/><p>x</p>")
    assert "<div/>" not in r.html and "<div>" in r.html and "x" in r.html


def test_sanitize_foreignobject_dropped_with_content():
    """D6：SVG 内嵌 HTML 容器整节点丢弃（预算图只许纯 SVG）。"""
    r = sanitize_html('<svg><foreignObject><div>inner</div></foreignObject>'
                      '<circle r="5"/></svg>')
    assert "inner" not in r.html and "<circle" in r.html


def test_render_and_inspect_passes_console_errors(tmp_path, monkeypatch):
    """render_and_inspect 透传 console_errors（修正轮修复信号），成功/失败两路。"""
    import tripmate.tools.htmlpdf as hpdf

    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "mini.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(pdf)
    doc.close()

    errs = ["ReferenceError: x is not defined"]

    def _fake_render(html_path, pdf_path, **kw):
        return {"ok": True, "engine": "playwright", "pdf_path": str(pdf),
                "console_errors": errs, "error": None}

    monkeypatch.setattr(hpdf, "render_html_pdf", _fake_render)
    html = tmp_path / "x.html"
    html.write_text("<p>x</p>", encoding="utf-8")
    diag = hpdf.render_and_inspect(html, tmp_path / "o.pdf",
                                   keywords=("不存在的关键词Ω",))
    assert diag["console_errors"] == errs  # 关键词缺失也不丢信号

    def _fake_fail(html_path, pdf_path, **kw):
        return {"ok": False, "engine": "none", "pdf_path": "",
                "console_errors": errs, "error": "boom"}

    monkeypatch.setattr(hpdf, "render_html_pdf", _fake_fail)
    bad = hpdf.render_and_inspect(html, tmp_path / "o2.pdf")
    assert bad["console_errors"] == errs and bad["error"] == "boom"


def test_inspect_days_page_band(tmp_path):
    """D3 页数警告带 2..days*6+6；不给 days 沿用 40 页固定上限。"""
    import tripmate.tools.htmlpdf as hpdf

    fitz = pytest.importorskip("fitz")

    def _pdf(n: int, name: str) -> Path:
        p = tmp_path / name
        doc = fitz.open()
        for _ in range(n):
            doc.new_page()
        doc.save(p)
        doc.close()
        return p

    many = _pdf(50, "many.pdf")
    assert any("50" in w for w in hpdf.inspect_pdf(many, keywords=(), days=2)["warnings"])
    assert hpdf.inspect_pdf(many, keywords=(), days=10)["warnings"] == []
    assert hpdf.inspect_pdf(many, keywords=())["warnings"] != []  # 默认 40 上限仍生效
