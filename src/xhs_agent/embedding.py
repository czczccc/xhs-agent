"""文本向量化：OpenAI 兼容的 /embeddings 接口（通义、智谱、硅基流动等），以及测试用的 FakeEmbedder。

向量维度不在配置里写死：建表时（xhs-db init）先调一次接口探测维度。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

import httpx

from .config import Settings


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatEmbedder:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        dimensions: int | None = None,
        batch_size: int = 10,  # 通义 text-embedding-v3/v4 单次最多 10 条
        timeout: float = 60,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.dimensions = dimensions
        self.batch_size = batch_size
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            payload: dict = {"model": self.model, "input": texts[i : i + self.batch_size], "encoding_format": "float"}
            if self.dimensions:
                payload["dimensions"] = self.dimensions
            resp = self._client.post("/embeddings", json=payload)
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            out.extend(d["embedding"] for d in data)
        return out


class FakeEmbedder:
    """字符 bigram 哈希到固定维度并归一化。相近文本的向量也相近，足够测试检索排序。"""

    model = "fake-embed"

    def __init__(self, dim: int = 64):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            t = "".join(t.split())
            v = [0.0] * self.dim
            for i in range(len(t) - 1):
                h = int(hashlib.md5(t[i : i + 2].encode("utf-8")).hexdigest(), 16)
                v[h % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / norm for x in v])
        return vecs


def build_embedder(settings: Settings) -> Embedder:
    missing = [k for k, v in {
        "EMBED_BASE_URL": settings.embed_base_url,
        "EMBED_API_KEY": settings.embed_api_key,
        "EMBED_MODEL": settings.embed_model,
    }.items() if not v]
    if missing:
        raise RuntimeError(f"pgvector 检索需要 embedding 接口，请在 .env 里填写：{', '.join(missing)}")
    return OpenAICompatEmbedder(
        settings.embed_base_url, settings.embed_api_key, settings.embed_model, settings.embed_dimensions
    )


def note_text(title: str, body: str, tags: list[str]) -> str:
    """笔记入库时用来算向量的文本：标题 + 标签 + 正文前 500 字。"""
    return f"{title}\n{' '.join(tags)}\n{body[:500]}"


def to_pgvector(vec: list[float]) -> str:
    """转成 pgvector 的文本字面量，SQL 里配合 %s::vector 使用，不需要额外注册类型。"""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"
