"""相似笔记检索。

- MemoryRetriever：读取 data/sample_notes.jsonl，用字符 bigram 的 Jaccard 相似度排序。零依赖，适合本地开发。
- PgVectorRetriever：Postgres + pgvector 余弦检索（需要 `pip install -e .[pg]`、embedding 接口，并先 xhs-db init / import）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .config import Settings
from .embedding import Embedder, to_pgvector
from .schemas import ReferenceNote


class Retriever(Protocol):
    def search(self, query: str, k: int = 3) -> list[ReferenceNote]: ...


def _bigrams(text: str) -> set[str]:
    text = "".join(text.split())
    return {text[i : i + 2] for i in range(len(text) - 1)}


class MemoryRetriever:
    def __init__(self, path: Path):
        self.notes = [
            ReferenceNote(**json.loads(line))
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def search(self, query: str, k: int = 3) -> list[ReferenceNote]:
        q = _bigrams(query)
        scored = []
        for n in self.notes:
            d = _bigrams(n.title + " ".join(n.tags) + n.body[:100])
            score = len(q & d) / (len(q | d) or 1)
            scored.append(n.model_copy(update={"score": round(score, 4)}))
        scored.sort(key=lambda n: (n.score, n.likes), reverse=True)
        return scored[:k]


class PgVectorRetriever:
    """Postgres + pgvector 余弦检索。连接池见 db.make_pool，向量来自 embedding 接口。"""

    SQL = """
        SELECT title, body, tags, likes, category, collects, comments, shop_type, content_type, author_type, city,
               1 - (embedding <=> %(v)s::vector) AS score
        FROM notes WHERE embedding IS NOT NULL
        ORDER BY embedding <=> %(v)s::vector LIMIT %(k)s
    """

    def __init__(self, pool, embedder: Embedder):
        self.pool = pool
        self.embedder = embedder

    def search(self, query: str, k: int = 3) -> list[ReferenceNote]:
        vec = to_pgvector(self.embedder.embed([query])[0])
        with self.pool.connection() as conn:
            rows = conn.execute(self.SQL, {"v": vec, "k": k}).fetchall()
        return [
            ReferenceNote(**{**r, "tags": list(r["tags"] or []), "likes": r["likes"] or 0, "score": round(float(r["score"]), 4)})
            for r in rows
        ]


def build_retriever(settings: Settings, pool=None) -> Retriever:
    if settings.retriever == "pgvector":
        from .db import make_pool
        from .embedding import build_embedder

        return PgVectorRetriever(pool or make_pool(settings), build_embedder(settings))
    return MemoryRetriever(settings.sample_notes)
