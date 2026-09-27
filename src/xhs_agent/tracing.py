"""结构化日志：每个节点一行 JSON，记录耗时、token、成败，写入 logs/runs.jsonl。"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


class Tracer:
    def __init__(self, log_file: Path):
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(self, **record) -> None:
        record.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    @contextmanager
    def step(self, run_id: str, node: str) -> Iterator[dict]:
        """用法：with tracer.step(run_id, "plan") as rec: rec["prompt_tokens"] = ..."""
        rec: dict = {"run_id": run_id, "node": node}
        start = time.perf_counter()
        try:
            yield rec
            rec["ok"] = True
        except Exception as e:  # 记录后继续抛出
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            rec["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
            self.log(**rec)


def summarize(log_file: Path, run_id: str) -> dict:
    """汇总某次运行的总耗时和 token。"""
    total = {"latency_ms": 0.0, "prompt_tokens": 0, "completion_tokens": 0, "steps": 0}
    if not log_file.exists():
        return total
    for line in log_file.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("run_id") != run_id:
            continue
        total["steps"] += 1
        total["latency_ms"] += r.get("latency_ms", 0)
        total["prompt_tokens"] += r.get("prompt_tokens", 0)
        total["completion_tokens"] += r.get("completion_tokens", 0)
    return total
