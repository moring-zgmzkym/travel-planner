"""搜索适配层（§4.3 信息收集 Agent 的工具）：攻略搜索 + 图片搜索。

真实通道：Tavily API（含 site: 限定）；未配置 Key 或调用失败 → 降级模拟数据（标注参考值，§7）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime

import httpx

from ..config import SearchConfig
from ..mocks.data import mock_guide_digest
from .resilience import ServiceUnavailable, with_retry

TAVILY_URL = "https://api.tavily.com/search"


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
        (f"{destination} 攻略 site:xiaohongshu.com", "小红书"),
        (f"{destination} 旅游攻略 site:mafengwo.cn", "马蜂窝"),
        (f"{destination} 旅游攻略 {month_hint}".strip(), "百度"),
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
    """图片搜索（§5.2）：Wikimedia Commons 真实图源（免 Key、CC 授权实拍图）；
    下载失败/无结果 → 本地 PIL 示意配图（明确标注非实景）。"""
    items = []
    async with httpx.AsyncClient(timeout=20.0, headers={"User-Agent": "TripMate/1.0 (course project)"},
                                 follow_redirects=True) as client:
        for spot in spots[:8]:
            got = await _commons_images(client, spot, per_spot)
            items.extend(got)
    if len(items) >= min(3, len(spots)):
        return {"mode": "real", "items": items}
    # 降级：本地 PIL 示意配图
    from .imagegen import generate_placeholder
    items = []
    for spot in spots[:8]:
        path = generate_placeholder(spot)
        items.append({"spot": spot, "path": path, "source": "本地示意配图（模拟数据模式，非实景）"})
    return {"mode": "mock", "notice": "图片搜索通道暂不可用，已生成本地示意配图（非实景，仅示意）", "items": items}


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
    except Exception:  # noqa: BLE001 — 图源失败降级
        return []


async def _download_image(client: httpx.AsyncClient, spot: str, url: str) -> str | None:
    """下载图片到 outputs/images（PDF 嵌入用），失败返回 None。"""
    from ..config import IMAGE_DIR
    ext = ".jpg" if ".jpg" in url.lower() or ".jpeg" in url.lower() else ".png"
    name = hashlib.md5(f"{spot}|{url}".encode()).hexdigest()[:16] + ext
    path = IMAGE_DIR / name
    if path.exists():
        return str(path)
    try:
        r = await client.get(url)
        r.raise_for_status()
        if len(r.content) < 5000:  # 过小的图（占位/图标）不要
            return None
        path.write_bytes(r.content)
        return str(path)
    except Exception:  # noqa: BLE001
        return None


def digest_to_model(digest: list[dict]) -> str:
    """攻略摘要压缩为提示词友好文本（控制 token，风险 #2）。"""
    return json.dumps(digest, ensure_ascii=False)[:6000]
