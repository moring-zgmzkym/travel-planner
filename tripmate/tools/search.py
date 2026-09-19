"""搜索适配层（§4.3 信息收集 Agent 的工具）：攻略搜索 + 图片搜索。

真实通道：Tavily API（含 site: 限定；图片检索 include_images）；Wikimedia Commons 备用图源。
两者皆不可得 → 本地 PIL 示意配图（标注非实景，§7 降级）。失败点全部记日志，不静默。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from ..config import SearchConfig
from ..mocks.data import mock_guide_digest
from .resilience import with_retry

logger = logging.getLogger("tripmate.tools.search")

TAVILY_URL = "https://api.tavily.com/search"

# 图片链路：浏览器 UA 提高部分图床防盗链的通过率；单请求 10s 快速失败（景点级并行兜底）
_IMG_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_IMG_TIMEOUT_S = 10.0
_PLACEHOLDER_SOURCE = "本地示意配图（模拟数据模式，非实景）"
# Commons 熔断（2026-09-19）：部分网络环境无法访问 commons.wikimedia.org（大陆网络实测
# 403/直连超时），每次封面/景点兜底都白等 2×12s——首次真实网络错误后进程内跳过，
# 重启恢复。测试注入失败后需手动复位该标记。
_COMMONS_DISABLED = False


def _trip_commons_breaker(exc: Exception) -> None:
    """Commons 网络错误 → 熔断置位（仅 ConnectTimeout/HTTPStatusError 这类真实网络错误）。"""
    global _COMMONS_DISABLED
    import httpx as _httpx
    if isinstance(exc, (_httpx.ConnectTimeout, _httpx.HTTPStatusError)):
        if not _COMMONS_DISABLED:
            logger.warning("Commons 不可达（%s: %s），本次运行内跳过后续 Commons 兜底", type(exc).__name__, exc)
        _COMMONS_DISABLED = True


# 已知水印图库（实拍图几乎必带水印/编号，混入 PDF 观感差，2026-08-30 汉中实测）：检索阶段直接跳过
_WATERMARK_HOSTS = ("huitu.com", "vcg.com", "nipic.com", "58pic.com", "dfic.cn", "sipaphoto.com")
# 优先图源：权威媒体/官方/百科（实景相关性与画质实测更稳，2026-08-30：光明网/中国日报/界面/西部网等）
_PREFERRED_HOSTS = ("chinadaily.com", "gmw.cn", "jiemian.com", "people.com.cn", "xinhuanet.com",
                    "cctv.com", "cnwest.com", "gov.cn", "wikimedia.org", "wikipedia.org",
                    "thepaper.cn", "chinanews.com", "ce.cn", "china.com.cn")


async def _tavily(client: httpx.AsyncClient, query: str, search_depth: str = "basic", max_results: int = 5) -> dict:
    """Tavily 文本搜索。客户端由调用方传入（共享连接池，8 路查询不再各开 8 个客户端）。"""

    async def _call() -> dict:
        r = await client.post(
            TAVILY_URL,
            json={
                "api_key": SearchConfig.TAVILY_API_KEY,
                "query": query,
                "search_depth": search_depth,
                "max_results": max_results,
                "include_answer": True,
            },
        )
        r.raise_for_status()
        return r.json()

    return await with_retry(_call, timeout_s=SearchConfig.TIMEOUT_S, retries=SearchConfig.RETRIES,
                            delay_s=SearchConfig.RETRY_DELAY_S, what=f"Tavily 搜索「{query}」")


def _degraded_notice() -> str:
    return "搜索引擎通道暂不可用（未配置 Key 或调用失败），以下为降级参考数据（§7 降级方案）"


def _guide_queries(destination: str, month_hint: str = "", style_hint: str = "") -> list[tuple[str, str]]:
    """攻略检索查询构造（纯函数，便于单测）：站点 3 路 + 主题 5 路。"""
    dest = destination or ""
    topic = f"{dest} {style_hint}".strip() if style_hint else dest
    return [
        (f"{dest} 攻略 site:xiaohongshu.com", "小红书检索"),
        (f"{dest} 旅游攻略 site:mafengwo.cn", "马蜂窝检索"),
        (f"{dest} 旅游攻略 {month_hint}".strip(), "全网检索"),
        (f"{dest} 美食攻略 必吃", "美食专题"),
        (f"{dest} 旅游 避坑 注意事项", "避坑专题"),
        (f"{dest} 行程路线 动线 {month_hint}".strip(), "路线专题"),
        (f"{topic} 必去景点 推荐", "景点专题"),
        (f"{dest} 出片 拍照机位 机位攻略", "机位专题"),
    ]


async def search_guides(destination: str, month_hint: str = "", style_hint: str = "") -> dict:
    """多类查询（§4.3）：站点限定 + 主题专题共 8 路并行（2026-09-19 扩容：新增出片机位专题路，
    服务《出片之旅》路书配图与行程取向），每路 top8 去重合并。
    8 路共享一个 httpx 客户端（连接复用）+ 信号量限 3 并发（平滑 Tavily 突发，与图片链路一致）。"""
    queries = _guide_queries(destination or "", month_hint, style_hint)
    if SearchConfig.TAVILY_API_KEY:
        sem = asyncio.Semaphore(3)

        async def _one(q: str) -> dict:
            async with sem:
                return await _tavily(client, q, max_results=8)

        async with httpx.AsyncClient(timeout=SearchConfig.TIMEOUT_S) as client:
            results = await asyncio.gather(*[_one(q) for q, _ in queries],
                                           return_exceptions=True)
        digest = []
        for (query, name), res in zip(queries, results):
            if isinstance(res, Exception):
                logger.warning("攻略检索「%s」失败（%s: %s）", name, type(res).__name__, res)
                continue  # 单来源失败标记暂缺并继续（§4.3）
            try:
                answer = res.get("answer", "")
                tops = res.get("results", [])[:8]
                if not tops:
                    continue
                digest.append({
                    "source_name": f"{name}（Tavily 搜索摘要级）",
                    "source_url": tops[0]["url"] if tops else "",
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "raw_answer": answer[:2000],
                    "raw_titles": [t.get("title", "")[:120] for t in tops],
                    "raw_urls": [t.get("url", "") for t in tops],
                    "reference_only": False,
                })
            except Exception as e:  # noqa: BLE001 — 单路响应畸形（results=null/条目缺键等）只降级该路，不丢其余已成功结果
                logger.warning("攻略检索「%s」响应解析失败（%s: %s），跳过该路", name, type(e).__name__, e)
        if digest:
            return {"mode": "real", "digest": digest}
    # 降级：模拟攻略摘要（结构化四元 + 来源）
    return {"mode": "mock", "notice": _degraded_notice(), "digest": mock_guide_digest(destination, month_hint)}


async def search_images(spots: list[str], per_spot: int = 2, city: str = "") -> dict:
    """图片搜索（§5.2）：Tavily 实景图为主源（带城市上下文双查询交叉，降低图文不符率）
    → Wikimedia Commons 备用 → 本地 PIL 示意配图。

    景点级并行（信号量限 3，防图源限流）；真图与占位可混排、逐图标注来源。
    返回 mode：全实拍=real / 部分实拍=mixed / 全占位=mock（§7 降级标注）。
    """
    names = [n for n in (s.strip() for s in spots[:8]) if n]
    async with httpx.AsyncClient(timeout=_IMG_TIMEOUT_S, headers=_IMG_HEADERS,
                                 follow_redirects=True) as client:
        sem = asyncio.Semaphore(3)

        async def _fetch(spot: str) -> list[dict]:
            async with sem:
                try:
                    return await _spot_images(client, spot, per_spot, city)
                except Exception as exc:  # noqa: BLE001 — 单景点失败不拖垮整体，降级占位
                    logger.warning("景点「%s」配图失败（%s: %s），使用本地示意配图",
                                   spot, type(exc).__name__, exc)
                    return [_placeholder(spot)]

        results = await asyncio.gather(*[_fetch(s) for s in names])
    items = [it for res in results for it in res]
    real_n = sum(1 for it in items if it["source"] != _PLACEHOLDER_SOURCE)
    if real_n == 0:
        return {"mode": "mock",
                "notice": "图片通道暂不可用（Tavily/Wikimedia 均未取到实拍图），已生成本地示意配图（非实景，仅示意）",
                "items": items}
    if real_n < len(items):
        return {"mode": "mixed",
                "notice": f"{len(items)} 张配图中 {real_n} 张为实拍图，其余为本地示意（非实景）",
                "items": items}
    return {"mode": "real", "items": items}


async def _commons_covers(client: httpx.AsyncClient, city: str, count: int) -> list[str]:
    """Commons 封面兜底（2026-09-19）：Tavily include_images 对城市封面查询基本只返回图库站
    缩略预览（实测成都 10 候选全部 350-900px，被 1200px 门槛全拒），Commons 原始分辨率正图
    是唯一稳定正图源。取原图 url（不传 iiurlwidth），沿用 ≥20000B / ≥1200px / 横版门槛。"""
    from ..config import IMAGE_DIR

    if _COMMONS_DISABLED:
        return []
    api = "https://commons.wikimedia.org/w/api.php"
    for gsrsearch in (f"{city} skyline", city):
        if _COMMONS_DISABLED:  # 熔断置位后立即短路（含同一次调用的剩余查询）
            return []
        params = {
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": f"filetype:bitmap {gsrsearch}", "gsrlimit": "10", "gsrnamespace": "6",
            "prop": "imageinfo", "iiprop": "url|mime|size",
        }
        try:
            r = await client.get(api, params=params)
            r.raise_for_status()
            pages = (r.json().get("query") or {}).get("pages") or {}
        except Exception as exc:  # noqa: BLE001 — 单源失败记日志后降级
            _trip_commons_breaker(exc)
            logger.warning("Commons 封面检索「%s」失败（%s: %s）", gsrsearch, type(exc).__name__, exc)
            continue
        out: list[str] = []
        for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
            info = (p.get("imageinfo") or [{}])[0]
            if info.get("mime") not in ("image/jpeg", "image/png"):
                continue
            if info.get("width", 0) < 1200 or info.get("width", 0) <= info.get("height", 0):
                continue  # 门槛前置：不够宽/竖图不下载
            url = info.get("url")
            if not url:
                continue
            local = await _download_image(client, f"{city}封面", url)
            if not local:
                continue
            try:
                from PIL import Image as PILImage
                with PILImage.open(local) as im:
                    if im.size[0] < 1200 or im.size[0] <= im.size[1]:
                        continue
                    path = IMAGE_DIR / ("citycover_" + hashlib.md5(
                        f"{city}|{url}".encode()).hexdigest()[:16] + ".jpg")
                    im.convert("RGB").save(path, quality=90)
                out.append(str(path))
                if len(out) >= count:
                    return out
            except Exception:  # noqa: BLE001 — 单张落盘失败换下一张
                continue
        if out:
            return out
    return []


async def search_city_covers(city: str, count: int = 3) -> list[str]:
    """城市宣传图（PDF 封面背景专用，2026-09-03）：独立查询词与内页素材图区分，
    PIL 门槛（宽 ≥1200 且横版）逐张验证，`citycover_` 前缀独立落盘；失败返回空列表。
    2026-09-19 增补 Commons 兜底：Tavily 图源对封面查询命中图库缩略图，门槛全拒。"""
    import io

    from PIL import Image as PILImage

    from ..config import IMAGE_DIR
    if not city:
        return []  # Commons 兜底无需 Key：mock 模式（无 Tavily Key）也能出真封面（2026-09-19）
    queries = [f"{city} 地标 城市风光 宣传照", f"{city} 城市天际线 摄影"]
    urls: list[str] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=_IMG_TIMEOUT_S, headers=_IMG_HEADERS,
                                 follow_redirects=True) as client:
        if SearchConfig.TAVILY_API_KEY:
            for q in queries:
                try:
                    r = await client.post(TAVILY_URL, json={
                        "api_key": SearchConfig.TAVILY_API_KEY,
                        "query": q, "search_depth": "basic",
                        "max_results": 6, "include_images": True,
                    })
                    r.raise_for_status()
                    imgs = r.json().get("images") or []
                except Exception as exc:  # noqa: BLE001 — 单查询失败继续另一查询
                    logger.warning("封面图检索「%s」失败（%s: %s）", q, type(exc).__name__, exc)
                    continue
                for u in (x.get("url") if isinstance(x, dict) else x for x in imgs):
                    if u and u not in seen and not any(h in urlsplit(u).netloc for h in _WATERMARK_HOSTS):
                        seen.add(u)
                        urls.append(u)
        out: list[str] = []
        for url in urls[:count + 6]:
            try:
                r = await client.get(url)
                r.raise_for_status()
                if len(r.content) < 20000:  # 封面全幅铺底，小图拉伸发糊
                    continue
                with PILImage.open(io.BytesIO(r.content)) as im:
                    im = im.convert("RGB")
                    w, h = im.size
                    if w < 1200 or w <= h:  # 够宽且横版（竖图裁竖版封面会太窄）
                        continue
                    path = IMAGE_DIR / ("citycover_" + hashlib.md5(
                        f"{city}|{url}".encode()).hexdigest()[:16] + ".jpg")
                    im.save(path, quality=90)
                out.append(str(path))
                if len(out) >= count:
                    break
            except Exception:  # noqa: BLE001 — 单候选失败换下一个
                continue
        if len(out) < count:
            # Tavily 图源对封面查询命中图库缩略图（2026-09-19 实测成都 0 张合格）→ Commons 正图兜底
            out += await _commons_covers(client, city, count - len(out))
    logger.info("封面宣传图检索「%s」：%d 张合格候选", city, len(out))
    return out


async def _spot_images(client: httpx.AsyncClient, spot: str, per_spot: int, city: str = "") -> list[dict]:
    """单景点抓取链：Tavily 实景图 → Wikimedia 备用 → PIL 占位。"""
    out = await _tavily_images(client, spot, per_spot, city)
    if len(out) < per_spot:
        out += await _commons_images(client, spot, per_spot - len(out))
    if out:
        return out
    logger.warning("景点「%s」未取到实拍图（Tavily/Wikimedia 均无结果），使用本地示意配图", spot)
    return [_placeholder(spot)]


def _placeholder(spot: str) -> dict:
    from .imagegen import generate_placeholder
    return {"spot": spot, "path": generate_placeholder(spot), "source": _PLACEHOLDER_SOURCE}


async def _tavily_images(client: httpx.AsyncClient, spot: str, per_spot: int, city: str = "") -> list[dict]:
    """Tavily 图片检索：双查询交叉（带城市/不带城市）扩候选，权威图源优先，逐个下载验证。

    双查询降低图文不符率（2026-08-30 用户反馈：图片与景点不符——单查询命中无关配图）；
    候选合并去重后按「权威媒体/官方优先」稳定排序；部分图链防盗链/失效，多备几个。"""
    if not SearchConfig.TAVILY_API_KEY:
        return []
    # 双查询：A 实景保底 + B 出片机位增强（《出片之旅》路书取向，2026-09-19）；B 无候选不影响 A 填充
    queries = [f"{spot} {city} 实景".strip(), f"{spot} 出片 机位 摄影"]
    urls: list[str] = []
    seen: set[str] = set()
    for q in queries:
        try:
            r = await client.post(TAVILY_URL, json={
                "api_key": SearchConfig.TAVILY_API_KEY,
                "query": q,
                "search_depth": "basic",
                "max_results": 5,
                "include_images": True,
            })
            r.raise_for_status()
            imgs = r.json().get("images") or []
        except Exception as exc:  # noqa: BLE001 — 单查询失败记日志，继续另一查询
            logger.warning("Tavily 图片检索「%s」失败（%s: %s）", q, type(exc).__name__, exc)
            continue
        for u in (x.get("url") if isinstance(x, dict) else x for x in imgs):  # 兼容新旧两种返回形状
            if u and u not in seen and not any(h in urlsplit(u).netloc for h in _WATERMARK_HOSTS):
                seen.add(u)
                urls.append(u)
    logger.info("Tavily 图片检索「%s」合并候选 %d 张", spot, len(urls))
    urls.sort(key=_host_rank)  # 稳定排序：权威图源排前，其余保持检索顺序
    out: list[dict] = []
    for url in urls[:per_spot + 4]:
        local = await _download_image(client, spot, url)
        if local:
            host = urlsplit(url).netloc or "web"
            out.append({"spot": spot, "path": local, "source": f"实景图（{host}，经 Tavily 检索）"})
        if len(out) >= per_spot:
            break
    return out


def _host_rank(url: str) -> int:
    """权威图源域名优先（稳定排序，非权威保持原相对顺序）。"""
    host = urlsplit(url).netloc
    return 0 if any(h in host for h in _PREFERRED_HOSTS) else 1


async def _commons_images(client: httpx.AsyncClient, spot: str, per_spot: int) -> list[dict]:
    """Wikimedia Commons 文件搜索：真实实拍图，CC 授权要求署名（我们逐图标注来源）。"""
    if _COMMONS_DISABLED:
        return []
    api = "https://commons.wikimedia.org/w/api.php"
    params = {
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"filetype:bitmap {spot}", "gsrlimit": str(per_spot + 2), "gsrnamespace": "6",
        "prop": "imageinfo", "iiprop": "url|mime|size", "iiurlwidth": "800",
    }
    try:
        r = await client.get(api, params=params)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
        out = []
        for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
            info = (p.get("imageinfo") or [{}])[0]
            if info.get("mime") not in ("image/jpeg", "image/png") or info.get("width", 0) < 500:
                continue
            thumb = info.get("thumburl") or info.get("url")
            if not thumb:
                continue
            local = await _download_image(client, spot, thumb)
            if local:
                out.append({"spot": spot, "path": local, "source": p.get("imageinfo", [{}])[0].get(
                    "descriptionurl") or "https://commons.wikimedia.org"})
            if len(out) >= per_spot:
                break
        return out
    except Exception as exc:  # noqa: BLE001 — 单图源失败记日志后降级
        _trip_commons_breaker(exc)
        logger.warning("Wikimedia 图片检索「%s」失败（%s: %s）", spot, type(exc).__name__, exc)
        return []


async def _download_image(client: httpx.AsyncClient, spot: str, url: str) -> str | None:
    """下载图片到 outputs/images（PDF 嵌入用），失败返回 None。"""
    from ..config import IMAGE_DIR
    name = hashlib.md5(f"{spot}|{url}".encode()).hexdigest()[:16] + _guess_ext(url)
    path = IMAGE_DIR / name
    if path.exists():
        return str(path)
    try:
        r = await client.get(url)
        r.raise_for_status()
        if len(r.content) < 5000:  # 过小的图（占位/图标）不要
            logger.debug("图片过小跳过（%d B）：%s", len(r.content), url[:120])
            return None
        path.write_bytes(r.content)
        return str(path)
    except Exception as exc:  # noqa: BLE001 — 单候选失败不影响其余候选
        logger.debug("图片下载失败（%s: %s）：%s", type(exc).__name__, exc, url[:120])
        return None


def _guess_ext(url: str) -> str:
    """图链常无后缀：默认 .jpg（Web 实景图绝大多数为 JPEG，reportlab 经 PIL 按内容识别）。"""
    low = url.lower().split("?")[0]
    if ".png" in low:
        return ".png"
    return ".jpg"


def digest_to_model(digest: list[dict]) -> str:
    """攻略摘要压缩为提示词友好文本（控制 token，风险 #2）。7 路扩容后上限同步放宽。"""
    return json.dumps(digest, ensure_ascii=False)[:9000]
