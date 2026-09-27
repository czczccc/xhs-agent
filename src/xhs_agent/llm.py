"""LLM 客户端。

- OpenAICompatLLM：调用任意 OpenAI 兼容的 /chat/completions 接口。
- FakeLLM：不联网、输出固定，用于测试和没配 API Key 时跑通流程。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    sanitized: int = 0  # 从输出里删掉的控制标记个数（见 strip_control_tokens）


class LLM(Protocol):
    model: str

    def complete(self, system: str, user: str, json_mode: bool = False) -> LLMResult: ...


# 模型控制标记，如 <|ad|>、<｜end▁of▁sentence｜>（DeepSeek 用全角竖线）。
# 有的中转服务偶尔会把这类标记混进输出，会弄坏 JSON，也不能出现在店主的笔记里。
_CONTROL_TOKEN = re.compile(r"<[|｜][^<>|｜\n]{1,40}[|｜]>")


def strip_control_tokens(text: str) -> tuple[str, int]:
    cleaned, n = _CONTROL_TOKEN.subn("", text)
    return cleaned, n


class OpenAICompatLLM:
    """stream=True 时用流式请求再拼回完整文本（有的中转服务只支持流式）。"""

    def __init__(
        self, base_url: str, api_key: str, model: str, timeout: float = 60, stream: bool = False,
        transport: httpx.BaseTransport | None = None,
    ):
        self.model = model
        self.stream = stream
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def complete(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.8,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        text, usage = self._stream(payload) if self.stream else self._once(payload)
        text, n = strip_control_tokens(text)
        return LLMResult(
            text=text,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            sanitized=n,
        )

    def _once(self, payload: dict) -> tuple[str, dict]:
        resp = self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"], data.get("usage") or {}

    def _stream(self, payload: dict) -> tuple[str, dict]:
        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        parts: list[str] = []
        usage: dict = {}
        with self._client.stream("POST", "/chat/completions", json=payload) as resp:
            if resp.is_error:
                resp.read()
                resp.raise_for_status()
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                usage = chunk.get("usage") or usage
                for choice in chunk.get("choices") or []:
                    parts.append((choice.get("delta") or {}).get("content") or "")
        return "".join(parts), usage


class FakeLLM:
    """按 system prompt 里的 [task:xxx] 标记返回固定内容。"""

    model = "fake"

    def complete(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        task = re.search(r"\[task:(\w+)\]", system)
        topic = re.search(r"(?:选题|内容)[:：]\s*(.+)", user)
        topic_text = topic.group(1).strip() if topic else "示例选题"
        short = topic_text[:8]
        if task and task.group(1) == "plan":
            out = {
                "angle": f"从真实体验出发讲清楚「{short}」",
                "title_candidates": [
                    f"{short}｜我踩过的坑都在这",
                    f"新手必看：{short}",
                    f"{short}，照着做就行",
                ],
                "outline": ["开头：一句话痛点", "正文：3 个关键步骤", "结尾：互动提问"],
                "tags": ["干货分享", "经验总结", short],
                "cover_text": short,
            }
            text = json.dumps(out, ensure_ascii=False)
        elif task and task.group(1) == "write":
            out = {
                "title": f"{short}｜我踩过的坑都在这"[:20],
                "body": (
                    f"姐妹们，关于{topic_text}，我总结了 3 个关键点👇\n\n"
                    "1️⃣ 先想清楚目标：别一上来就照搬别人的方案，先写下自己最想解决的一个问题。\n"
                    "2️⃣ 找对方法：从最简单的一步开始，做完一步再加下一步，节奏比完美更重要。\n"
                    "3️⃣ 坚持复盘：每周花十分钟记录哪里顺手、哪里卡住，下周只改一个地方。\n\n"
                    "你们还有什么想问的，评论区见～"
                ),
                "tags": ["干货分享", "经验总结", short],
            }
            text = json.dumps(out, ensure_ascii=False)
        elif task and task.group(1) == "ask":
            out = {"questions": [f"「{short}」最想让客人记住的一个细节是什么？", "有没有哪位老客让你印象很深？"]}
            text = json.dumps(out, ensure_ascii=False)
        else:
            text = "{}"
        return LLMResult(text=text, prompt_tokens=len(system) + len(user), completion_tokens=len(text))


def build_llm(settings: Settings) -> LLM:
    if settings.llm_api_key:
        return OpenAICompatLLM(settings.llm_base_url, settings.llm_api_key, settings.llm_model, stream=settings.llm_stream)
    return FakeLLM()


def parse_json(text: str) -> dict:
    """兼容模型把 JSON 包在 ```json 代码块里、或在 JSON 前后多输出文字的情况。"""
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    text = m.group(1) if m else text
    start = text.find("{")
    if start < 0:
        return json.loads(text)
    obj, _ = json.JSONDecoder().raw_decode(text, start)
    return obj
