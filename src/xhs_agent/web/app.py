"""最小 Web Demo：输入选题 → 查看草稿和封面 → 通过 / 驳回 / 提修改意见。

运行：uvicorn xhs_agent.web.app:app --reload
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .. import uploads
from ..cover import STYLES
from ..feedback import EventIn
from ..graph import XhsAgent
from ..nodes.steps import render_cover_for
from ..publish import PublishError, publish_note
from ..schemas import NoteRequest, ShopProfile
from ..shops import Shop
from ..tracing import summarize

app = FastAPI(title="小红书图文生成 Agent")
_agent: XhsAgent | None = None


def agent() -> XhsAgent:
    global _agent
    if _agent is None:
        _agent = XhsAgent()
    return _agent


class Decision(BaseModel):
    decision: str  # approve / reject / revise
    feedback: str = ""


class PublishConfirm(BaseModel):
    confirm: bool = False  # 后端也校验一次，不只靠前端弹窗


def _view(state: dict) -> dict:
    run_id = state["run_id"]
    return {
        "run_id": run_id,
        "status": state.get("status"),
        "error": state.get("error"),
        "plan": state["plan"].model_dump() if state.get("plan") else None,
        "draft": state["draft"].model_dump() if state.get("draft") else None,
        "review": state["review"].model_dump() if state.get("review") else None,
        "references": [r.model_dump() for r in state.get("references", [])],
        # v=attempts：重写后封面文字会变，换个 URL 避免浏览器用旧缓存
        "cover_url": f"/api/runs/{run_id}/cover?v={state.get('attempts', 0)}" if state.get("cover_path") else None,
        "cover_style": state["request"].cover_style,
        "photos": [f"/api/uploads/{p}" for p in state["request"].photos],
        "stats": summarize(agent().settings.log_file, run_id),
        "publish_enabled": agent().settings.publish_enabled,
        "published": state.get("published", False),
        "publish_url": state.get("publish_url") or None,
    }


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    """给店主用的手机端页面。"""
    return (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")


@app.get("/debug", response_class=HTMLResponse)
def debug() -> str:
    """开发调试页：通用模式 + 商家模式，显示参考笔记和 token 统计。"""
    return (Path(__file__).parent / "templates" / "debug.html").read_text(encoding="utf-8")


# ---------- 试用码 ----------
# 前端把试用码放在 X-Shop-Code 请求头。REQUIRE_SHOP_CODE=false 时（本地调试）不校验、不限额。


def current_shop(x_shop_code: str | None = Header(None)) -> Shop | None:
    if not agent().settings.require_shop_code:
        return None
    shop = agent().shops.get(x_shop_code or "")
    if not shop:
        raise HTTPException(401, "试用码不对，请检查一下")
    if not shop.active:
        raise HTTPException(401, "这个试用码已停用，有问题找给你试用码的人")
    return shop


def _prepare(req: NoteRequest, shop: Shop | None) -> NoteRequest:
    """用服务端存的店铺档案替换前端传来的，并记上试用码。"""
    if shop is None:
        return req
    if shop.profile is None:
        raise HTTPException(409, "请先填写店铺档案")
    return req.model_copy(update={"shop": shop.profile, "shop_code": shop.code})


def _consume(shop: Shop | None) -> None:
    if shop is not None and not agent().shops.consume(shop.code):
        raise HTTPException(429, f"今天的 {shop.daily_limit} 次已经用完了，明天再来吧")


def _own(run_id: str, shop: Shop | None) -> dict:
    """只能看/改自己店的运行；别人的一律当作不存在。"""
    state = agent().get(run_id)
    if not state or (shop is not None and state["request"].shop_code != shop.code):
        raise HTTPException(404, "run not found")
    return state


@app.get("/api/shop")
def get_shop(shop: Shop | None = Depends(current_shop)) -> dict:
    if shop is None:
        raise HTTPException(400, "未启用试用码")
    return shop.model_dump(exclude={"active"})


@app.put("/api/shop")
def put_shop(profile: ShopProfile, shop: Shop | None = Depends(current_shop)) -> dict:
    if shop is None:
        raise HTTPException(400, "未启用试用码")
    agent().shops.save_profile(shop.code, profile)
    return agent().shops.get(shop.code).model_dump(exclude={"active"})


@app.post("/api/questions")
def questions(req: NoteRequest, shop: Shop | None = Depends(current_shop)) -> dict:
    """素材追问：商家模式下先拿到要问店主的问题；通用模式或生成失败时返回空列表。不计额度。"""
    return {"questions": agent().ask(_prepare(req, shop))}


@app.post("/api/runs")
def create_run(req: NoteRequest, shop: Shop | None = Depends(current_shop)) -> dict:
    req = _prepare(req, shop)
    _consume(shop)
    return _view(agent().start(req))


@app.get("/api/runs/{run_id}")
def get_run(run_id: str, shop: Shop | None = Depends(current_shop)) -> dict:
    return _view(_own(run_id, shop))


@app.post("/api/runs/{run_id}/decision")
def decide(run_id: str, body: Decision, shop: Shop | None = Depends(current_shop)) -> dict:
    _own(run_id, shop)
    if body.decision == "revise":
        _consume(shop)
    try:
        return _view(agent().resume(run_id, body.decision, body.feedback))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


# ---------- 发布到小红书：通过外部 xiaohongshu-mcp（浏览器自动化，非官方 API）一键发帖 ----------
# 不在生成流程里自动触发；必须店主在结果页看过草稿、点了「确认发布」才会调用，且发布后不可撤回。


@app.get("/api/config")
def get_config() -> dict:
    return {"publish_enabled": agent().settings.publish_enabled}


@app.post("/api/runs/{run_id}/publish")
async def publish(run_id: str, body: PublishConfirm, request: Request, shop: Shop | None = Depends(current_shop)) -> dict:
    settings = agent().settings
    if not settings.publish_enabled:
        raise HTTPException(400, "还没配置发布功能")
    if not body.confirm:
        raise HTTPException(400, "需要先确认才能发布")
    state = _own(run_id, shop)
    if state.get("published"):
        raise HTTPException(409, "这篇已经发布过了，不会重复发")
    if state.get("status") != "pending_review" or not (state.get("review") and state["review"].passed):
        raise HTTPException(409, "这篇还没通过审核，不能发布")
    if not state.get("cover_path"):
        raise HTTPException(409, "还没有封面，不能发布")

    draft, req = state["draft"], state["request"]
    base = settings.public_base_url or str(request.base_url).rstrip("/")
    images = [f"{base}/api/runs/{run_id}/cover?style={req.cover_style}"] + [f"{base}/api/uploads/{p}" for p in req.photos]

    try:
        result = await publish_note(settings, title=draft.title, content=draft.body, tags=draft.tags, images=images)
    except PublishError as e:
        agent().tracer.log(run_id=run_id, node="publish", ok=False, error=str(e))
        raise HTTPException(502, str(e)) from e

    agent().tracer.log(run_id=run_id, node="publish", ok=True, post_url=result.post_url)
    agent().mark_published(run_id, result.post_url)
    return {"ok": True, "message": result.message, "post_url": result.post_url}


# ---------- 流式版本：每完成一个节点推一条 SSE，前端据此显示真实进度 ----------


def _sse(events) -> StreamingResponse:
    def gen():
        try:
            for kind, value in events:
                payload = {"type": "node", "node": value} if kind == "node" else {"type": "done", "run": _view(value)}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001 — 流已经开始，只能用事件告诉前端
            yield f"data: {json.dumps({'type': 'error', 'message': f'{type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/runs/stream")
def create_run_stream(req: NoteRequest, shop: Shop | None = Depends(current_shop)) -> StreamingResponse:
    req = _prepare(req, shop)
    _consume(shop)
    return _sse(agent().start_stream(req))


@app.post("/api/runs/{run_id}/decision/stream")
def decide_stream(run_id: str, body: Decision, shop: Shop | None = Depends(current_shop)) -> StreamingResponse:
    _own(run_id, shop)
    try:
        events = agent().resume_stream(run_id, body.decision, body.feedback)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.decision == "revise":
        _consume(shop)
    return _sse(events)


@app.get("/api/runs/{run_id}/cover")
def cover(run_id: str, style: str | None = None) -> FileResponse:
    """封面 PNG。style 不传就是生成时选的样式；换样式时按需渲染并缓存。"""
    state = agent().get(run_id)
    if not state or not state.get("cover_path"):
        raise HTTPException(404, "cover not found")
    if style is None or style == state["request"].cover_style:
        path = Path(state["cover_path"])
    elif style in STYLES:
        path = render_cover_for(agent().settings, state, style)
    else:
        raise HTTPException(400, f"未知封面样式：{style}")
    return FileResponse(path, media_type="image/png", filename=f"cover_{run_id}.png", content_disposition_type="inline")


@app.post("/api/runs/{run_id}/events")
def event(run_id: str, body: EventIn, shop: Shop | None = Depends(current_shop)) -> dict:
    """店主的评分和使用行为（复制、保存图片、换封面样式）。"""
    if body.kind == "rating" and not body.rating:
        raise HTTPException(400, "rating 事件需要 rating 字段")
    _own(run_id, shop)
    agent().record_event(run_id, body)
    return {"ok": True}


# 封面和照片的 GET 不校验试用码：<img> 标签带不了请求头，靠不可猜的 id（运行 48 位、照片 128 位随机）保护


@app.post("/api/uploads")
async def upload(file: UploadFile, shop: Shop | None = Depends(current_shop)) -> dict:
    """店主上传照片。返回 id，生成请求里放进 photos。"""
    data = await file.read(uploads.MAX_BYTES + 1)
    try:
        photo_id = uploads.save_upload(agent().settings.output_dir, data)
    except uploads.UploadError as e:
        raise HTTPException(400, str(e)) from e
    return {"id": photo_id, "url": f"/api/uploads/{photo_id}"}


@app.get("/api/uploads/{photo_id}")
def get_upload(photo_id: str) -> FileResponse:
    try:
        path = uploads.path_for(agent().settings.output_dir, photo_id)
    except uploads.UploadError as e:
        raise HTTPException(400, str(e)) from e
    if not path.exists():
        raise HTTPException(404, "photo not found")
    return FileResponse(path, media_type="image/jpeg")
