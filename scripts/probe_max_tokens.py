"""一次性诊断（2026-09-19）：走 autogen create 路径验证 max_tokens 16384 的可用性与效果。

假设：思考 token 计入 max_tokens=8192，重负载下推理耗尽预算 → content 为空
（e2e 与用户会话实测：结构化双空/群聊消息被清洗成占位）。对比 8192 vs 16384。
用后可删。"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autogen_core.models import UserMessage  # noqa: E402

from tripmate.digest import _STRUCT_PROMPT, _STRUCT_ROW_CHARS  # noqa: E402
from tripmate.llm import get_model_client  # noqa: E402
from tripmate.tools.search import search_guides  # noqa: E402

_THINK_LOW = {"extra_create_args": {"extra_body": {"thinking": {"type": "enabled", "effort": "low"}}}}


async def main() -> None:
    r = await search_guides("成都", "十一", style_hint="休闲 美食")
    texts = []
    for i, d in enumerate(r["digest"], 1):
        a = str(d.get("raw_answer") or "").strip()
        t = "；".join(x for x in (d.get("raw_titles") or []) if x)
        texts.append(f"【{i}】{d.get('source_name', '')}\n标题：{t}\n原文：{a[:_STRUCT_ROW_CHARS]}")
    prompt = _STRUCT_PROMPT.format(city="成都", corpus="\n".join(texts)[:12000])
    client = get_model_client()

    for mt in (8192, 16384):
        args = dict(_THINK_LOW)
        args["extra_create_args"] = {**args["extra_create_args"], "max_tokens": mt}
        t0 = time.time()
        try:
            result = await client.create([UserMessage(content=prompt, source="probe")], **args)
            dt = time.time() - t0
            content = result.content if isinstance(result.content, str) else ""
            print(f"max_tokens={mt}: {dt:5.1f}s finish={result.finish_reason} "
                  f"content_len={len(content)} rows={'有' if '\"rows\"' in content or content.strip().startswith('[') else '无'}")
        except Exception as e:
            print(f"max_tokens={mt}: EXC {type(e).__name__}: {str(e)[:110]}")


if __name__ == "__main__":
    asyncio.run(main())
