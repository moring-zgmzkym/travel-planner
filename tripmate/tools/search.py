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
from .resilience import ServiceUnavailable, with_retry

logger = logging.getLogger("tripmate.tools.search")

TAVILY_URL = "https://api.tavily.com/search"

# 图片链路：浏览器 UA 提高部分图床防盗链的通过率；单请求 10s 快速失败（景点级并行兜底）
_IMG_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
_IMG_TIMEOUT_S = 10.0
_PLACEHOLDER_SOURCE = "本地示意配图（模拟数据模式，非实景）"


async def _tavily(query: str, search_depth: str = "basic") -> dict:
    async def _call() -> dict:
        async with httpx.AsyncClient(timeout=SearchConfig.TIMEOUT_S) as client:
            r = await client.post(
                TAVILY_URL,
                json={
                    "api_key": SearchConfig.TAVILY_API_KEY,
                    "query": query,
                    "search_depth": search_depth,
                    "max_results": 5,
                    "include_answer": True,
                },
            )
            r.raise_for_status()
            return r.json()

    return await with_retry(_call, timeout_s=SearchConfig.TIMEOUT_S, retries=SearchConfig.RETRIES,
                            delay_s=SearchConfig.RETRY_DELAY_S, what=f"Tavily 搜索「{query}」")


def _degraded_notice() -> str:
    return "搜索引擎通道暂不可用（未配置 Key 或调用失败），以下为降级参考数据（§7 降级方案）"


async def search_guides(destination: str, month_hint: str = "") -> dict:
    """多类查询（§4.3）：小红书/马蜂窝 site: 限定 + 百度常规，每类 top5 去重合并。"""
    queries = [
        (f"{destination} 攻略 site:xiaohongshu.com", "小红书检索"),
        (f"{destination} 旅游攻略 site:mafengwo.cn", "马蜂窝检索"),
        (f"{destination} 旅游攻略 {month_hint}".strip(), "全网检索"),
    ]
    if SearchConfig.TAVILY_API_KEY:
        try:
            results = await asyncio.gather(*[_tavily(q) for q, _ in queries], return_exceptions=True)
            digest = []
            for (query, name), res in zip(queries, results):
                if isinstance(res, Exception):
                    continue  # 单来源失败标记暂缺并继续（§4.3）
                answer = res.get("answer", "")
                tops = res.get("results", [])[:5]
                digest.append({
                    "source_name": f"{name}（Tavily 搜索摘要级）",
                    "source_url": tops[0]["url"] if tops else "",
                    "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "raw_answer": answer[:2000],
                    "raw_titles": [t.get("title", "")[:120] for t in tops],
                    "raw_urls": [t.get("url", "") for t in tops],
                    "reference_only": False,
                })
            if digest:
                return {"mode": "real", "digest": digest}
        except ServiceUnavailable:
            pass
    # 降级：模拟攻略摘要（结构化四元 + 来源）
    return {"mode": "mock", "notice": _degraded_notice(), "digest": mock_guide_digest(destination, month_hint)}


async def search_images(spots: list[str], per_spot: int = 2) -> dict:
    """图片搜索（§5.2）：Tavily 实景图为主源 → Wikimedia Commons 备用 → 本地 PIL 示意配图。

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
                    return await _spot_images(client, spot, per_spot)
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


async def _spot_images(client: httpx.AsyncClient, spot: str, per_spot: int) -> list[dict]:
    """单景点抓取链：Tavily 实景图 → Wikimedia 备用 → PIL 占位。"""
    out = await _tavily_images(client, spot, per_spot)
    if len(out) < per_spot:
        out += await _commons_images(client, spot, per_spot - len(out))
    if out:
        return out
    logger.warning("景点「%s」未取到实拍图（Tavily/Wikimedia 均无结果），使用本地示意配图", spot)
    return [_placeholder(spot)]


def _placeholder(spot: str) -> dict:
    from .imagegen import generate_placeholder
    return {"spot": spot, "path": generate_placeholder(spot), "source": _PLACEHOLDER_SOURCE}


async def _tavily_images(client: httpx.AsyncClient, spot: str, per_spot: int) -> list[dict]:
    """Tavily 图片检索（include_images）：实景图候选逐个下载验证（部分图链防盗链/失效，多备几个）。"""
    if not SearchConfig.TAVILY_API_KEY:
        return []
    try:
        r = await client.post(TAVILY_URL, json={
            "api_key": SearchConfig.TAVILY_API_KEY,
            "query": f"{spot} 实景 照片",
            "search_depth": "basic",
            "max_results": 5,
            "include_images": True,
        })
        r.raise_for_status()
        imgs = r.json().get("images") or []
    except Exception as exc:  # noqa: BLE001 — 单图源失败记日志后降级 Wikimedia
        logger.warning("Tavily 图片检索「%s」失败（%s: %s）", spot, type(exc).__name__, exc)
        return []
    urls = [u.get("url") if isinstance(u, dict) else u for u in imgs]  # 兼容新旧两种返回形状
    logger.info("Tavily 图片检索「%s」返回 %d 张候选", spot, len(urls))
    out: list[dict] = []
    for url in urls[:per_spot + 4]:
        if not url:
            continue
        local = await _download_image(client, spot, url)
        if local:
            host = urlsplit(url).netloc or "web"
            out.append({"spot": spot, "path": local, "source": f"实景图（{host}，经 Tavily 检索）"})
        if len(out) >= per_spot:
            break
    return out


async def _commons_images(client: httpx.AsyncClient, spot: str, per_spot: int) -> list[dict]:
    """Wikimedia Commons 文件搜索：真实实拍图，CC 授权要求署名（我们逐图标注来源）。"""
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
    """攻略摘要压缩为提示词友好文本（控制 token，风险 #2）。"""
    return json.dumps(digest, ensure_ascii=False)[:6000]
