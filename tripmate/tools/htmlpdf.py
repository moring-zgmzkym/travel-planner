"""HTML 消毒 / 套壳 / PDF 渲染 / 渲染诊断（方向二确定性工具链，docs/html-designer-plan.md D2/D3）。

设计约束（审核修订条文，勿退化）：
- 消毒 = 允许列表（allowlist）重建，禁止黑名单正则剥标签（大小写/嵌套拼接绕过是经典漏洞）
- file:// 白名单：URI 解析回本机路径必须位于 IMAGE_DIR（outputs/images，含 crops），UNC 一律拒绝
- data: URI 仅允许 image/png|jpeg base64，单图与全文档限量
- <a href> 仅放行 http(s)（订单直达链接是黑板数据；<a> 不触发资源加载），其余剥 href 留文本
- <style> 文本与 style 属性单独清洗：禁 @import；url() 仅允许白名单 file URI 与 data:image
- 渲染：每次独立 sync_playwright 上下文（sync API 线程亲和，禁止模块级单例复用）+ 进程级
  Semaphore 串行 + goto/pdf 各自超时；Playwright 失败降级 Edge CLI（独立 --user-data-dir，
  超时 taskkill 整棵进程树），产物事后校验 %PDF 头（Edge 失败可能静默）
- 本模块不得 import autogen（保证无 autogen 环境可全量单测）
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from ..config import IMAGE_DIR
from ..design import DEFAULT_THEME, THEMES, load_print_css
from ..status import AUDIT

# ---- 限额（D2 限额表）----
MAX_HTML_BYTES = 512 * 1024          # Agent 片段大小上限（超限判交付失败进重试）
MAX_IMAGE_COUNT = 40                 # 图片引用数上限
MAX_DATA_URI_BYTES = 2 * 1024 * 1024     # 单个 data URI 解码后上限
MAX_DATA_URI_TOTAL = 4 * 1024 * 1024     # 全文档 data URI 解码总量上限

# 允许列表：标签 → 额外允许的属性（class/id/style/title 全局放行）
_ALLOWED_ATTRS: dict[str, set[str]] = {
    "section": set(), "header": set(), "footer": set(), "main": set(),
    "div": set(), "p": set(), "span": set(), "a": {"href", "target"},
    "h1": set(), "h2": set(), "h3": set(), "h4": set(), "h5": set(), "h6": set(),
    "ul": set(), "ol": set(), "li": set(),
    "table": set(), "thead": set(), "tbody": set(), "tr": set(),
    "th": {"colspan", "rowspan"}, "td": {"colspan", "rowspan"},
    "figure": set(), "figcaption": set(), "img": {"src", "alt", "width", "height"},
    "strong": set(), "em": set(), "b": set(), "i": set(), "br": set(), "hr": set(),
    "style": set(),
    "svg": {"viewbox", "width", "height", "preserveaspectratio", "xmlns"},
    "g": {"transform", "fill", "stroke", "font-size", "font-weight"},
    "path": {"d", "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-dashoffset",
             "stroke-linecap", "transform", "opacity"},
    "circle": {"cx", "cy", "r", "fill", "stroke", "stroke-width", "stroke-dasharray",
               "stroke-dashoffset", "stroke-linecap", "transform", "opacity"},
    "ellipse": {"cx", "cy", "rx", "ry", "fill", "stroke", "stroke-width", "transform"},
    "rect": {"x", "y", "width", "height", "rx", "ry", "fill", "stroke", "stroke-width", "transform", "opacity"},
    "line": {"x1", "y1", "x2", "y2", "stroke", "stroke-width", "transform"},
    "polyline": {"points", "fill", "stroke", "stroke-width"},
    "polygon": {"points", "fill", "stroke", "stroke-width"},
    "text": {"x", "y", "text-anchor", "font-size", "font-weight", "fill", "transform"},
    "tspan": {"x", "y", "font-size", "fill"},
    "use": {"href", "xlink:href"},
    "defs": set(),
    "lineargradient": {"id", "x1", "y1", "x2", "y2", "gradientunits"},
    "stop": {"offset", "stop-color", "stop-opacity"},
    "title": set(),
}
_GLOBAL_ATTRS = {"class", "id", "style", "title"}
_VOID = {"br", "hr", "img"}
# _DROP_WITH_CONTENT 中的 void 标签（无结束标签）：只记数不置 _drop_depth，
# 否则后续全文被静默吞掉（LLM 偶发 <meta> 即致整篇丢失还烧重试预算）
_DROP_VOID = {"meta", "link", "base", "embed", "source", "input"}
# 整节点连同内容删除的标签（HTMLParser 对其内容按 CDATA/普通处理，需显式跳过）
_DROP_WITH_CONTENT = {"script", "iframe", "noscript", "object", "embed", "applet",
                      "form", "template", "head", "base", "meta", "link",
                      "source", "picture", "button", "input", "select", "textarea",
                      "foreignobject"}  # SVG 内嵌 HTML 容器（D6：预算图只许纯 SVG，禁 foreignObject）

_CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
_CSS_IMPORT_RE = re.compile(
    r"@import\s+(?:url\([^)]*\)|\"[^\"]*\"|'[^']*')[^;{}]*;?", re.IGNORECASE)
_DATA_URI_RE = re.compile(r"^data:image/(png|jpeg);base64,", re.IGNORECASE)


class HtmlTooLargeError(ValueError):
    """Agent 交付片段超过大小上限（判交付失败进重试）。"""


@dataclass
class SanitizeResult:
    html: str
    findings: list[str] = field(default_factory=list)
    removed_nodes: int = 0

    @property
    def clean(self) -> bool:
        return not self.findings


def _image_root() -> Path:
    """file:// 引用白名单根目录（outputs/images，含 crops 子目录）。"""
    return IMAGE_DIR.resolve()


def _file_uri_in_whitelist(uri: str) -> bool:
    """file:/// URI 是否指向 IMAGE_DIR 白名单内；UNC（netloc 非空）直接拒绝。"""
    try:
        u = urlparse(uri)
    except ValueError:
        return False
    if u.scheme != "file":
        return False
    if u.netloc:  # file://server/share 形式 = UNC，SMB 凭据外泄面，一律拒绝
        return False
    # url2pathname 正确处理 Windows 盘符（/D:/x → D:\x）；url2pathname 自带百分号解码
    path = Path(url2pathname(u.path))
    try:
        resolved = path.resolve()
        return resolved == _image_root() or _image_root() in resolved.parents
    except OSError:
        return False


def _data_uri_ok(uri: str, state_total: list[int]) -> bool:
    m = _DATA_URI_RE.match(uri)
    if not m:
        return False
    b64 = uri.split(",", 1)[-1] if "," in uri else ""
    size = len(b64) * 3 // 4
    if size > MAX_DATA_URI_BYTES or state_total[0] + size > MAX_DATA_URI_TOTAL:
        return False  # 被拒者不计入总量（评审 #9：不殃及后续合法图片）
    state_total[0] += size
    return True


_CSS_ESCAPE_RE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\n]?|\\(.)", re.DOTALL)


def _css_unescape(css: str) -> str:
    """解码 CSS 转义（评审 #2）：`\\69 mport` → `import`，防转义绕过 @import 剥除；
    本设计系统 print.css 无合法反斜杠用例，解码不损伤正常样式。"""

    def _repl(m: re.Match) -> str:
        if m.group(1):
            try:
                return chr(int(m.group(1), 16))
            except (ValueError, OverflowError):
                return ""
        return m.group(2) or ""

    return _CSS_ESCAPE_RE.sub(_repl, css)


def _css_clean(css: str, findings: list[str]) -> str:
    """<style> 文本 / style 属性清洗：禁 @import；url() 仅允许白名单 file URI 与 data:image。

    先解码 CSS 转义再匹配（评审 #2：`@\\69 mport` 形式的转义绕过）；
    再剥 `/* */` 注释（`u/**/rl(...)`、`@/**/import` 分割绕过对 CSS 词法无效，
    注释剥除后即现形）；残留 @import 一律 fail-closed（未知形态不保留）。
    """
    css = _css_unescape(css)
    css = _CSS_COMMENT_RE.sub("", css)
    css = _CSS_IMPORT_RE.sub("", css)
    if re.search(r"@import\b", css, re.IGNORECASE):
        findings.append("CSS 含无法识别形态的 @import，已整体丢弃该段样式")
        return ""

    def _sub(m: re.Match) -> str:
        val = (m.group(1) or "").strip()
        if val.startswith("data:") and _DATA_URI_RE.match(val):
            return m.group(0)
        if val.startswith("file:") and _file_uri_in_whitelist(val):
            return m.group(0)
        findings.append(f"CSS 外链资源已剥除：{val[:80]}")
        return "none"

    return _CSS_URL_RE.sub(_sub, css)


class _AllowlistParser(HTMLParser):
    """允许列表重建：白名单外标签整节点删除（内容一并丢弃），其余按白名单属性重建。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.findings: list[str] = []
        self.removed = 0
        self._drop_depth = 0            # _DROP_WITH_CONTENT 内待跳过的层级
        self._drop_current = False      # 当前元素整体剥除标记（img.src 校验失败）
        self._img_count = 0
        self._data_total = [0]          # data URI 累计解码字节
        self._style_depth = 0           # <style> 原文缓存
        self._style_buf: list[str] = []

    # -- 属性过滤 --
    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> list[tuple[str, str]]:
        allowed = _ALLOWED_ATTRS.get(tag, set()) | _GLOBAL_ATTRS
        out: list[tuple[str, str]] = []
        self._drop_current = False
        for name, value in attrs:
            name = name.lower()
            value = value if value is not None else ""
            if name.startswith("on"):
                self.findings.append(f"事件属性已剥除：{tag}.{name}")
                self.removed += 1
                continue
            if name not in allowed:
                if name == "srcset":
                    self.findings.append(f"srcset 已剥除：{tag}")
                    self.removed += 1
                continue
            low = value.strip().lower().replace("\n", "").replace("\t", "")
            if low.startswith("javascript:") or low.startswith("vbscript:"):
                self.findings.append(f"脚本 URI 已剥除：{tag}.{name}")
                self.removed += 1
                continue
            if tag == "img" and name == "src":
                if not self._img_src_ok(value):
                    self._drop_current = True  # 整元素剥除（不留坏图空壳）
                    continue
            elif tag == "a" and name == "href":
                if not value.strip().lower().startswith(("http://", "https://")):
                    self.findings.append(f"a.href 非 http(s) 已剥除：{value[:60]}")
                    self.removed += 1
                    continue
            elif name in ("href", "xlink:href"):
                if not value.strip().startswith("#"):  # SVG 内部引用放行，外部一律剥除
                    self.removed += 1
                    continue
            if name in ("fill", "stroke"):
                # SVG 表现属性外部 url()：仅 url(#...) 内部渐变引用合法，其余剥除
                compact = low.replace(" ", "")
                if "url(" in compact and "url(#" not in compact:
                    self.findings.append(f"SVG 外部资源引用已剥除：{tag}.{name}")
                    self.removed += 1
                    continue
            if name == "style":
                value = _css_clean(value, self.findings)
            out.append((name, value))
        return out

    def _img_src_ok(self, src: str) -> bool:
        """img.src 三态：白名单 file URI / 限额内 data:image / 其余剥除该图。"""
        self._img_count += 1
        if self._img_count > MAX_IMAGE_COUNT:
            self.findings.append(f"图片引用超过 {MAX_IMAGE_COUNT} 张上限，已剥除")
            self.removed += 1
            return False
        s = src.strip()
        if s.startswith("file:"):
            if _file_uri_in_whitelist(s):
                return True
            self.findings.append(f"file:// 图片不在白名单（限 outputs/images），已剥除：{s[:80]}")
            self.removed += 1
            return False
        if s.lower().startswith("data:"):
            if _data_uri_ok(s, self._data_total):
                return True
            self.findings.append("data URI 超类型/体积限制，已剥除")
            self.removed += 1
            return False
        self.findings.append(f"外链图片已剥除（离线渲染）：{s[:80]}")
        self.removed += 1
        return False

    # -- 重建 --
    def _emit_start(self, tag: str, attrs: list[tuple[str, str]]) -> None:
        # 属性值转义 & 与 "（评审 #11：保证消毒器输出是不动点）
        parts = [tag] + [f'{n}="{v.replace("&", "&amp;").replace(chr(34), "&quot;")}"'
                         for n, v in attrs]
        self.out.append(f"<{' '.join(parts)}>")

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DROP_WITH_CONTENT:
                self._drop_depth += 1
            return
        if tag in _DROP_WITH_CONTENT:
            self.removed += 1
            if tag not in _DROP_VOID:
                self._drop_depth = 1
            return
        if tag not in _ALLOWED_ATTRS:
            self.removed += 1
            if tag == "base":
                self.findings.append("<base> 已剥除（相对路径基准不可更改）")
            elif tag == "meta":
                self.findings.append("<meta> 已剥除（charset 由套壳统一写入）")
            return
        if tag == "style":
            self._style_depth += 1
            self._style_buf = []
            return
        clean = self._attrs(tag, attrs)
        if not self._drop_current:
            self._emit_start(tag, clean)

    def handle_startendtag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._drop_depth or tag in _DROP_WITH_CONTENT:
            return
        if tag not in _ALLOWED_ATTRS:
            self.removed += 1
            return
        clean = self._attrs(tag, attrs)
        if self._drop_current:
            return
        parts = [tag] + [f'{n}="{v.replace("&", "&amp;").replace(chr(34), "&quot;")}"'
                         for n, v in clean]
        if tag in _VOID:
            self.out.append(f"<{' '.join(parts)}/>")
        elif tag == "style":
            self.out.append(f"<style></style>")
        else:
            # 非 void 标签的自闭合写法（<div/>）：HTML 词法按普通开始标签处理，
            # 照此原样输出开始标签，避免凭空产生未闭合嵌套破坏版式
            self.out.append(f"<{' '.join(parts)}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop_depth:
            if tag in _DROP_WITH_CONTENT:
                self._drop_depth -= 1
            return
        if tag == "style" and self._style_depth:
            self._style_depth -= 1
            cleaned = _css_clean("".join(self._style_buf), self.findings)
            self.out.append(f"<style>{cleaned}</style>")
            return
        if tag in _ALLOWED_ATTRS and tag not in _VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        if self._style_depth:
            self._style_buf.append(data)
            return
        self.out.append(data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    def handle_comment(self, data: str) -> None:  # 注释整体丢弃（LLM 噪声/潜在向量）
        return

    def handle_decl(self, decl: str) -> None:
        return  # 文档骨架由套壳代码生成，片段内 DOCTYPE 丢弃


def sanitize_html(fragment: str) -> SanitizeResult:
    """对 Agent 交付的 body 片段做允许列表消毒（D2）。超限抛 HtmlTooLargeError。"""
    if len(fragment.encode("utf-8")) > MAX_HTML_BYTES:
        raise HtmlTooLargeError(f"HTML 片段超过 {MAX_HTML_BYTES // 1024}KB 上限")
    p = _AllowlistParser()
    p.feed(fragment)
    p.close()
    return SanitizeResult(html="".join(p.out), findings=p.findings, removed_nodes=p.removed)


def wrap_html(body: str, title: str, theme: str = DEFAULT_THEME) -> str:
    """确定性套壳：注入 DOCTYPE/charset/print.css/主题 class/页脚。

    骨架与字符集永不依赖 LLM 自觉（审核 #16）；print.css 全文内联，无相对路径问题；
    title 实体转义（评审 #3：dest 来自用户画像，不转义可逃逸 <title> 破坏骨架）。
    """
    if theme not in THEMES:
        theme = DEFAULT_THEME
    safe_title = (title.replace("&", "&amp;").replace("<", "&lt;")
                  .replace(">", "&gt;").replace('"', "&quot;"))
    footer = ("<footer class=\"doc-footer\"><span>本路书由 TripMate AI 版面设计师生成 · "
              "数据以规划画像为准，示意图已标注</span><span>TripMate · AI Designer</span></footer>")
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n<style>{load_print_css()}</style>\n</head>\n"
        f'<body class="{theme}">\n{body}\n{footer}\n</body>\n</html>'
    )


# ---------------------------------------------------------------------------
# 渲染（Playwright 主 / Edge CLI 备）与诊断
# ---------------------------------------------------------------------------

_RENDER_SEM = threading.Semaphore(1)   # 多会话并发 finalize 串行化（审核 #6）


def _discover_chromium() -> str | None:
    """内核版本漂移兜底：ms-playwright 下任意 chromium-* 可执行文件。"""
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if not root.exists():
        return None
    for pat in ("chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
                "chromium-*/chrome-win64/chrome.exe",
                "chromium-*/chrome-win/chrome.exe"):
        hits = sorted(root.glob(pat))
        if hits:
            return str(hits[-1])
    return None


def _find_edge() -> Path | None:
    for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
        base = os.environ.get(env)
        if base:
            p = Path(base) / "Microsoft/Edge/Application/msedge.exe"
            if p.exists():
                return p
    return None


def _render_playwright(html_path: Path, pdf_path: Path, goto_timeout_s: float) -> list[str]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=_discover_chromium())
        try:
            page = browser.new_page()
            console_errors: list[str] = []
            page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
            page.goto(html_path.resolve().as_uri(), wait_until="load", timeout=goto_timeout_s * 1000)
            # Spike 0.3 实测：@page 边距优先于 pdf() margin 参数，版式以 print.css 为准
            page.pdf(path=str(pdf_path), format="A4", print_background=True,
                     prefer_css_page_size=True, display_header_footer=False)
            return console_errors[:10]
        finally:
            browser.close()


def _render_playwright_bounded(html_path: Path, pdf_path: Path, goto_timeout_s: float,
                               pdf_timeout_s: float) -> list[str]:
    """page.pdf() 无 timeout 参数（1.58 实测）：daemon 线程 join 限时，超时即放弃并抛错。

    持锁线程永不被挂死阻塞——render_html_pdf 的 finally 照常释放信号量，渲染通道自愈；
    代价是挂死的浏览器线程残留（daemon，随进程退出回收；playwright 线程亲和，
    不可跨线程 close）。挂死是小概率事件，一次泄漏换整通道可用是合算的 trade-off。
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["result"] = _render_playwright(html_path, pdf_path, goto_timeout_s)
        except Exception as exc:  # noqa: BLE001 — 转交调用方统一进 Edge 兜底
            box["error"] = exc

    th = threading.Thread(target=_run, daemon=True)
    th.start()
    th.join(timeout=goto_timeout_s + pdf_timeout_s)
    if th.is_alive():
        AUDIT.output("HtmlPdf",
                     f"Playwright 渲染超时（>{goto_timeout_s + pdf_timeout_s:.0f}s），"
                     "已放弃等待（残留线程随进程退出回收），降级 Edge CLI")
        raise RuntimeError("Playwright 渲染超时")
    if "error" in box:
        raise box["error"]
    return box.get("result", [])


def _render_edge(html_path: Path, pdf_path: Path, timeout_s: float) -> bool:
    exe = _find_edge()
    if not exe:
        return False
    html_uri = html_path.resolve().as_uri()
    with tempfile.TemporaryDirectory(prefix="tm_edge_") as ud:
        cmd = [str(exe), "--headless", f"--user-data-dir={ud}", "--no-pdf-header-footer",
               "--disable-gpu", "--no-first-run", f"--print-to-pdf={pdf_path}", html_uri]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=0x08000000)  # CREATE_NO_WINDOW
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            # Windows 下只杀父进程必留僵尸（msedge 会 fork 多个子进程）——taskkill 整棵树
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
            return False
    return pdf_path.exists() and pdf_path.read_bytes()[:5] == b"%PDF-"


def _pdf_valid(pdf_path: Path) -> bool:
    try:
        return pdf_path.exists() and pdf_path.read_bytes()[:5] == b"%PDF-" and pdf_path.stat().st_size > 1000
    except OSError:
        return False


def render_html_pdf(html_path: str | Path, pdf_path: str | Path,
                    goto_timeout_s: float = 30, edge_timeout_s: float = 90,
                    sem_timeout_s: float = 120, pdf_timeout_s: float = 90) -> dict:
    """渲染 HTML → PDF：Playwright 主通道，Edge CLI 兜底；失败返回 ok=False（不抛）。

    同步重活，调用方必须 asyncio.to_thread 执行（内联会冻结事件循环，见 team.py 注释）。
    信号量获取限时（评审 #5）：其他会话的渲染挂死时不让后续定稿无限排队。
    """
    html_path, pdf_path = Path(html_path), Path(pdf_path)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if pdf_path.exists():
            pdf_path.unlink()  # Edge 失败可能静默复用旧产物——渲染前先清场
    except OSError as exc:  # Windows 下 PDF 阅读器锁定文件时发生；让后续渲染报清晰错误
        return {"ok": False, "engine": "none", "pdf_path": "", "console_errors": [],
                "error": f"PDF 产物被占用，无法覆盖（请关闭已打开的 PDF）：{exc}"}
    first_exc: Exception | None = None
    console_errors: list[str] = []
    if not _RENDER_SEM.acquire(timeout=sem_timeout_s):
        return {"ok": False, "engine": "none", "pdf_path": "", "console_errors": [],
                "error": "渲染通道被其他会话长时间占用，跳过本次渲染"}
    try:
        try:
            console_errors = _render_playwright_bounded(
                html_path, pdf_path, goto_timeout_s, pdf_timeout_s)
            if _pdf_valid(pdf_path):
                return {"ok": True, "engine": "playwright", "pdf_path": str(pdf_path),
                        "console_errors": console_errors, "error": None}
            raise RuntimeError("PDF 产物无效")
        except Exception as exc:  # noqa: BLE001 — 主通道任何失败都进兜底
            first_exc = exc
            AUDIT.output("HtmlPdf", f"Playwright 渲染失败，降级 Edge CLI：{type(exc).__name__}: {exc}")
            try:
                if _render_edge(html_path, pdf_path, edge_timeout_s) and _pdf_valid(pdf_path):
                    return {"ok": True, "engine": "edge", "pdf_path": str(pdf_path),
                            "console_errors": [], "error": None}
                return {"ok": False, "engine": "none", "pdf_path": "", "console_errors": [],
                        "error": f"playwright: {type(first_exc).__name__}: {first_exc}; "
                                 f"edge 兜底也失败"}
            except Exception as edge_exc:  # noqa: BLE001
                return {"ok": False, "engine": "none", "pdf_path": "", "console_errors": [],
                        "error": f"playwright: {type(first_exc).__name__}: {first_exc}; "
                                 f"edge: {type(edge_exc).__name__}: {edge_exc}"}
    finally:
        _RENDER_SEM.release()


def inspect_pdf(pdf_path: str | Path, keywords: tuple[str, ...] = ("行程", "预算", "订单"),
                max_pages: int = 40, days: int | None = None) -> dict:
    """确定性诊断（D3）：页数/关键词/溢出信号。PyMuPDF 缺失时优雅降级（审核 #15）。

    页数警告带按天数动态（2..days*6+6，超界仅警告不判失败）；days 未给出时
    沿用固定的 40 页参考上限。溢出块计数为 best-effort 信号，不作硬门槛；
    关键词缺失才判 not ok。
    """
    path = Path(pdf_path)
    if not _pdf_valid(path):
        return {"ok": False, "pages": 0, "missing_keywords": list(keywords),
                "overflow_blocks": 0, "error": "PDF 不存在或头非法"}
    try:
        import fitz
    except ImportError:
        return {"ok": True, "pages": -1, "missing_keywords": [], "overflow_blocks": 0,
                "degraded": True, "note": "PyMuPDF 未安装，跳过文本诊断"}
    with fitz.open(path) as doc:
        pages = doc.page_count
        text = "".join(p.get_text() for p in doc)
        overflow = 0
        for p in doc:
            rect = p.rect
            for b in p.get_text("blocks"):
                x0, y0, x1, y1 = b[:4]
                if x1 > rect.width + 2 or y1 > rect.height + 2 or x0 < -2 or y0 < -2:
                    overflow += 1
    missing = [k for k in keywords if k not in text]
    warnings = []
    page_cap = days * 6 + 6 if days else max_pages
    if pages > page_cap:
        warnings.append(f"页数 {pages} 超过参考上限 {page_cap}")
    if days and pages < 2:
        warnings.append(f"页数 {pages} 疑似过少（{days} 天行程）")
    if overflow:
        warnings.append(f"{overflow} 个文本块疑似越界（best-effort 信号）")
    return {"ok": not missing, "pages": pages, "missing_keywords": missing,
            "overflow_blocks": overflow, "warnings": warnings, "error": None}


def render_and_inspect(html_path: str | Path, pdf_path: str | Path,
                       keywords: tuple[str, ...] = ("行程", "预算", "订单"),
                       days: int | None = None) -> dict:
    """渲染 + 诊断一步完成（减少一次 LLM 往返）；返回有界 JSON（不回传全文）。

    console_errors 透传（Playwright 侧已截断 ≤10 条）：JS/资源错误是修正轮的
    关键修复信号，不可丢弃。
    """
    render = render_html_pdf(html_path, pdf_path)
    if not render["ok"]:
        return {"ok": False, "stage": "render", "error": render["error"],
                "console_errors": render.get("console_errors", [])}
    diag = inspect_pdf(pdf_path, keywords=keywords, days=days)
    diag["stage"] = "inspect"
    diag["engine"] = render["engine"]
    diag["pdf_path"] = render["pdf_path"]
    diag["console_errors"] = render.get("console_errors", [])
    return diag
