"""LangGraph 节点。每个节点接收 state，返回要更新的字段，并通过 tracer 记录耗时和 token。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from pydantic import ValidationError

from .. import prompts, review, uploads
from ..config import Settings
from ..cover import render_cover
from ..llm import LLM, parse_json
from ..retrieval import Retriever
from ..schemas import AgentState, ContentPlan, Draft, NoteRequest, Questions
from ..tracing import Tracer


@dataclass
class Deps:
    settings: Settings
    llm: LLM
    retriever: Retriever
    tracer: Tracer
    sensitive_words: list[str]
    merchant_words: list[str] = field(default_factory=list)  # 商家模式额外的广告法词表


class NodeError(RuntimeError):
    pass


def _call_json(deps: Deps, rec: dict, system: str, user: str, model_cls):
    """调用模型并校验成 model_cls。格式不对时把错误反馈给模型重试，最多 max_format_retries 次。"""
    prompt = user
    for i in range(deps.settings.max_format_retries + 1):
        res = deps.llm.complete(system, prompt, json_mode=True)
        rec["prompt_tokens"] = rec.get("prompt_tokens", 0) + res.prompt_tokens
        rec["completion_tokens"] = rec.get("completion_tokens", 0) + res.completion_tokens
        rec["model"] = deps.llm.model
        if getattr(res, "sanitized", 0):
            rec["sanitized"] = rec.get("sanitized", 0) + res.sanitized
        rec["format_retries"] = i
        try:
            return model_cls(**parse_json(res.text))
        except (json.JSONDecodeError, ValidationError, TypeError) as e:
            error = f"模型输出不符合 {model_cls.__name__} 结构：{e}；原始输出开头：{res.text[:300]!r}"
            prompt = f"{user}\n\n上一次输出无法使用：{e}\n请只输出一个符合字段要求的 JSON 对象，不要输出其他内容。"
    raise NodeError(error)


def _ref_line(r) -> str:
    """参考笔记在 prompt 里的一行：来源类型 + 互动数据 + 正文开头。"""
    label = "·".join(x for x in (r.author_type, r.content_type) if x)
    stats = f"{r.likes} 赞" + (f" / {r.collects} 收藏" if r.collects else "")
    return f"- 《{r.title}》（{label + '，' if label else ''}{stats}）{r.body[:80]}"


def _fail(e: Exception) -> dict:
    """LLM 相关节点失败时不抛异常，而是把运行标记为 error，交给图直接结束。"""
    return {"status": "error", "error": f"{type(e).__name__}: {e}"}


def render_cover_for(settings: Settings, state: AgentState, style: str, force: bool = False) -> Path:
    """按运行状态渲染封面 PNG：第一张照片 + 封面大字 + 副标题（商家模式是「店名 · 位置」）。

    同一运行每种样式一个文件；force=True 时重画（重写之后标题变了）。照片文件丢了就退回纯色封面。
    """
    req, run_id = state["request"], state["run_id"]
    out = settings.output_dir / f"{run_id}_cover_{style}.png"
    if out.exists() and not force:
        return out
    photo = None
    if req.photos:
        p = uploads.path_for(settings.output_dir, req.photos[0])
        photo = p if p.exists() else None
    if req.shop:
        subtitle = " · ".join(x for x in (req.shop.name, req.shop.area or req.shop.city) if x)
    else:
        subtitle = state["draft"].title
    return render_cover(state["plan"].cover_text, subtitle, out, photo, style, settings.cover_font or None)


def ask_questions(deps: Deps, run_id: str, req: NoteRequest) -> list[str]:
    """素材追问：动笔前生成 2-3 个问店主的问题。只用于商家模式；失败时返回空列表，直接生成即可。"""
    if not req.shop:
        return []
    user = prompts.ASK_USER.format(
        shop=req.shop.facts(), content_type=req.content_type or "日常", topic=req.topic, extra=req.extra or "无"
    )
    try:
        with deps.tracer.step(run_id, "ask") as rec:
            qs = _call_json(deps, rec, prompts.ASK_SYSTEM, user, Questions).questions
            rec["questions"] = len(qs)
    except (NodeError, httpx.HTTPError):
        return []
    return [q.strip() for q in qs if q.strip()]


def make_nodes(deps: Deps) -> dict:
    def retrieve(state: AgentState) -> dict:
        with deps.tracer.step(state["run_id"], "retrieve") as rec:
            req = state["request"]
            # 商家模式按「品类 + 内容类型 + 内容」检索，找同类店铺的笔记
            query = f"{req.shop.shop_type} {req.content_type or ''} {req.topic}" if req.shop else req.topic
            refs = deps.retriever.search(query, k=3)
            rec["hits"] = len(refs)
        return {"references": refs, "attempts": 0}

    def plan(state: AgentState) -> dict:
        req = state["request"]
        refs = "\n".join(_ref_line(r) for r in state.get("references", [])) or "（无）"
        if req.shop:
            system = prompts.MERCHANT_PLAN_SYSTEM
            user = prompts.MERCHANT_PLAN_USER.format(
                shop=req.shop.facts(), content_type=req.content_type or "日常", topic=req.topic,
                extra=req.extra or "无", style=req.style, references=refs, details=req.details() or "（无）",
            )
        else:
            system = prompts.PLAN_SYSTEM
            user = prompts.PLAN_USER.format(
                topic=req.topic, audience=req.audience, style=req.style, extra=req.extra or "无", references=refs
            )
        try:
            with deps.tracer.step(state["run_id"], "plan") as rec:
                p = _call_json(deps, rec, system, user, ContentPlan)
        except (NodeError, httpx.HTTPError) as e:
            return _fail(e)
        return {"plan": p}

    def write(state: AgentState) -> dict:
        feedback = ""
        if state.get("review") and not state["review"].passed:
            feedback = "上一版审核未通过，请修改：\n" + "\n".join(state["review"].issues)
        if state.get("human_decision") == "revise" and state.get("human_feedback"):
            feedback += "\n人工修改意见：" + state["human_feedback"]
        req = state["request"]
        if req.shop:
            system = prompts.MERCHANT_WRITE_SYSTEM
            user = prompts.MERCHANT_WRITE_USER.format(
                shop=req.shop.facts(), content_type=req.content_type or "日常", topic=req.topic,
                extra=req.extra or "无", plan=state["plan"].model_dump_json(), feedback=feedback,
                details=req.details() or "（无）",
            )
        else:
            system = prompts.WRITE_SYSTEM
            user = prompts.WRITE_USER.format(topic=req.topic, plan=state["plan"].model_dump_json(), feedback=feedback)
        try:
            with deps.tracer.step(state["run_id"], "write") as rec:
                rec["attempt"] = state.get("attempts", 0) + 1
                d = _call_json(deps, rec, system, user, Draft)
        except (NodeError, httpx.HTTPError) as e:
            return _fail(e)
        return {"draft": d, "attempts": state.get("attempts", 0) + 1, "human_decision": None}

    def check(state: AgentState) -> dict:
        with deps.tracer.step(state["run_id"], "review") as rec:
            req = state["request"]
            words = deps.sensitive_words + (deps.merchant_words if req.shop else [])
            r = review.check(state["draft"], words, facts=req.facts() or None)
            rec["passed"] = r.passed
            rec["issues"] = r.issues
        return {"review": r}

    def cover(state: AgentState) -> dict:
        with deps.tracer.step(state["run_id"], "cover") as rec:
            path = render_cover_for(deps.settings, state, state["request"].cover_style, force=True)
            rec["photo"] = bool(state["request"].photos)
        return {"cover_path": str(path), "status": "pending_review"}

    def give_up(state: AgentState) -> dict:
        deps.tracer.log(run_id=state["run_id"], node="give_up", issues=state["review"].issues)
        return {"status": "failed"}

    def finalize(state: AgentState) -> dict:
        decision = state.get("human_decision", "approve")
        deps.tracer.log(run_id=state["run_id"], node="finalize", decision=decision)
        return {"status": "approved" if decision == "approve" else "rejected"}

    return {
        "retrieve": retrieve,
        "plan": plan,
        "write": write,
        "review": check,
        "cover": cover,
        "give_up": give_up,
        "finalize": finalize,
    }
