"""生成 GitHub Pages 上的演示站 docs/demo/：真实前端 + 回放层，不需要后端。

- index.html 从 src/xhs_agent/web/templates/index.html 复制，前后各注入一个脚本：
  demo-pre.js 拦截 /api/* 请求，demo-post.js 把输码页换成「选一家示例店」等演示专用界面
- data.json 来自评测的真实生成结果（本地 evals/results/，未入库）：
    跳过追问 → v3 结果；回答了追问 → 带模拟店主回答的结果；「按意见重写」→ 同一需求 v2 的结果
- covers/：无照片时的封面（服务端同款渲染器生成）；有照片时演示页用 canvas 现画

用法：python scripts/build_demo.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xhs_agent.cover import render_cover  # noqa: E402

RESULTS = ROOT / "evals" / "results"
RUN_V3 = "20260927_203556"  # v3 prompt，跳过追问
RUN_INTERVIEW = "20260927_205203"  # v3 prompt + 追问（模拟店主）
RUN_V2 = "20260927_181156"  # v2 prompt，作为「重写」后的另一版
OUT = ROOT / "docs" / "demo"
EMOJI = re.compile(r"[\U0001F000-\U0001FFFF☀-➿️]")


def load(run: str) -> dict:
    lines = (RESULTS / run / "details.jsonl").read_text(encoding="utf-8").splitlines()
    return {r["id"]: r for r in map(json.loads, lines)}


def result(row: dict | None) -> dict | None:
    if not row or not row.get("draft"):
        return None
    d = row["draft"]
    return {"title": d["title"], "body": d["body"], "tags": d["tags"], "judge": row.get("judge")}


def cover_text(title: str) -> str:
    """封面大字：标题去掉 emoji，逗号处断行。"""
    t = EMOJI.sub("", title).strip()
    return re.sub(r"[，,、｜|]+", " ", t).strip()


def main() -> None:
    v3, iv, v2 = load(RUN_V3), load(RUN_INTERVIEW), load(RUN_V2)
    shops: dict[str, dict] = {}
    (OUT / "covers").mkdir(parents=True, exist_ok=True)
    for cid, row in v3.items():
        prof = row["shop"]
        shop = shops.setdefault(prof["name"], {"code": f"SHOP{len(shops) + 1:02d}", "profile": prof, "cases": []})
        skip = result(row)
        text = cover_text(skip["title"])
        sub = " · ".join(x for x in (prof["name"], prof.get("area") or prof.get("city")) if x)
        png = OUT / "covers" / f"{cid}.png"
        render_cover(text, sub, png)
        Image.open(png).resize((720, 960), Image.LANCZOS).save(png, optimize=True)
        answered = iv.get(cid) or {}
        shop["cases"].append({
            "id": cid,
            "content_type": row["content_type"],
            "topic": row["topic"],
            "extra": row.get("extra") or "",
            "cover_text": text,
            "questions": [{"q": a["question"], "a": a["answer"]} for a in answered.get("answers", [])],
            "skip": skip,
            "answered": result(answered),
            "revised": result(v2.get(cid)),
        })
    (OUT / "data.json").write_text(json.dumps({"shops": list(shops.values())}, ensure_ascii=False, indent=1), encoding="utf-8")

    page = (ROOT / "src" / "xhs_agent" / "web" / "templates" / "index.html").read_text(encoding="utf-8")
    assert page.count("<script>") == 1 and page.count("</script>") == 1
    head, tail = page.rsplit("</script>", 1)  # 先定位应用脚本的结尾，再在前面插 demo-pre
    page = head + '</script>\n<script src="demo-post.js"></script>' + tail
    page = page.replace("<script>", '<script src="demo-pre.js"></script>\n<script>', 1)
    page = page.replace("<title>店铺笔记助手</title>", "<title>店铺笔记助手 · 演示</title>")
    (OUT / "index.html").write_text(page, encoding="utf-8")
    n = sum(len(s["cases"]) for s in shops.values())
    print(f"demo: {len(shops)} 家店 / {n} 个需求 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
