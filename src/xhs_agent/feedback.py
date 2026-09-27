"""店主反馈和使用行为。

两类事件都存进同一张表 / 文件：
- 显式反馈 rating：ready（能直接发）/ edit（改改能发）/ bad（不能用），可附一句 comment；可以改主意，以最后一次为准
- 行为信号：copy_title / copy_body（复制了文案）、save_images（打开保存图片）、cover_style（切换封面样式）
  复制正文基本等于真要发了，比按钮更可信

STORAGE=postgres 存 events 表；memory 存 logs/events.jsonl。汇总见 summarize() 和 `xhs-db feedback`。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field

EventKind = Literal["rating", "copy_title", "copy_body", "save_images", "cover_style"]
RATINGS = {"ready": "能直接发", "edit": "改改能发", "bad": "不能用"}


class EventIn(BaseModel):
    """前端上报的事件。"""

    kind: EventKind
    rating: Literal["ready", "edit", "bad"] | None = None
    comment: str = Field("", max_length=500)
    value: str = Field("", max_length=50)  # 例如 cover_style 的样式名


class Event(EventIn):
    run_id: str
    shop_code: str | None = None  # 阶段 4 试用码
    shop_name: str = ""
    content_type: str = ""
    ts: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class EventStore(Protocol):
    def add(self, event: Event) -> None: ...

    def all(self) -> list[Event]: ...


class JsonlEventStore:
    def __init__(self, path: Path):
        self.path = path

    def add(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")

    def all(self) -> list[Event]:
        if not self.path.exists():
            return []
        return [Event.model_validate_json(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


class PgEventStore:
    COLS = ("run_id", "shop_code", "shop_name", "content_type", "kind", "rating", "comment", "value")

    def __init__(self, pool):
        self.pool = pool

    def add(self, event: Event) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                f"INSERT INTO events ({', '.join(self.COLS)}) VALUES ({', '.join('%s' for _ in self.COLS)})",
                [getattr(event, c) for c in self.COLS],
            )

    def all(self) -> list[Event]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT {', '.join(self.COLS)}, created_at FROM events ORDER BY id").fetchall()
        return [Event(**{c: r[c] for c in self.COLS if r[c] is not None}, ts=r["created_at"].isoformat()) for r in rows]


def summarize(events: list[Event]) -> dict:
    """按运行聚合：每篇取最后一次评分；再按店、按内容类型统计评分分布和复制率。"""
    runs: dict[str, dict] = defaultdict(lambda: {"rating": None, "comment": "", "copied": False, "saved": False})
    for e in events:
        r = runs[e.run_id]
        r.update(shop=e.shop_name or e.shop_code or "（未知）", content_type=e.content_type or "（未知）")
        if e.kind == "rating":
            r["rating"], r["comment"] = e.rating, e.comment
        elif e.kind in ("copy_title", "copy_body"):
            r["copied"] = True
        elif e.kind == "save_images":
            r["saved"] = True

    def stats(items: list[dict]) -> dict:
        rated = [i for i in items if i["rating"]]
        c = Counter(i["rating"] for i in rated)
        return {
            "runs": len(items),
            "rated": len(rated),
            **{RATINGS[k]: c.get(k, 0) for k in RATINGS},
            "ready_rate": round(c.get("ready", 0) / len(rated), 2) if rated else None,
            "copy_rate": round(sum(i["copied"] for i in items) / len(items), 2) if items else None,
        }

    by = lambda key: {k: stats([r for r in runs.values() if r[key] == k]) for k in sorted({r[key] for r in runs.values()})}  # noqa: E731
    return {
        "overall": stats(list(runs.values())),
        "by_shop": by("shop"),
        "by_content_type": by("content_type"),
        "comments": [
            {"run_id": k, "shop": r["shop"], "rating": RATINGS[r["rating"]], "comment": r["comment"]}
            for k, r in runs.items() if r["rating"] and r["comment"]
        ],
    }


def print_summary(s: dict) -> None:
    o = s["overall"]
    print(f"共 {o['runs']} 篇，{o['rated']} 篇有评分：" + "，".join(f"{k} {o[k]}" for k in RATINGS.values()))
    print(f"能直接发比例 {o['ready_rate']}，复制过文案的比例 {o['copy_rate']}")
    for title, key in (("按店", "by_shop"), ("按内容类型", "by_content_type")):
        print(f"\n{title}：")
        for name, st in s[key].items():
            print(f"  {name}: {st['runs']} 篇，评分 {st['rated']}，能直接发 {st['能直接发']}，复制率 {st['copy_rate']}")
    if s["comments"]:
        print("\n店主的原话：")
        for c in s["comments"]:
            print(f"  [{c['shop']}·{c['rating']}] {c['comment']}")
