"""LangGraph 工作流。

    retrieve → plan → write → review ─通过→ cover → [人工确认] ─approve/reject→ finalize
                        ↑         │                     │
                        └─未通过且可重试                └─revise（带修改意见）→ write
                                  └─重试用尽→ give_up

plan / write 调用模型失败（格式重试用尽或网络错误）时 status=error，图直接结束。

人工确认用 interrupt_before 实现：图在 human_gate 前暂停，状态存在 checkpointer 里，
Web/CLI 拿到用户决定后用 resume() 继续。
"""

from __future__ import annotations

import uuid
from typing import Iterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, StateGraph

from .config import Settings
from .feedback import Event, EventIn, EventStore, JsonlEventStore, PgEventStore
from .shops import JsonShopStore, PgShopStore, ShopStore
from .llm import LLM, build_llm
from .nodes.steps import Deps, ask_questions, make_nodes
from .retrieval import Retriever, build_retriever
from .review import load_words
from .schemas import AgentState, NoteRequest
from .tracing import Tracer


# checkpoint 里会存这些 Pydantic 对象，需显式放行反序列化
_STATE_TYPES = [
    ("xhs_agent.schemas", name)
    for name in ("NoteRequest", "ShopProfile", "Answer", "ReferenceNote", "ContentPlan", "Draft", "ReviewResult")
]


def _serde() -> JsonPlusSerializer:
    return JsonPlusSerializer(allowed_msgpack_modules=_STATE_TYPES)


def default_checkpointer() -> MemorySaver:
    """STORAGE=memory：进程重启后未确认的运行会丢失。"""
    return MemorySaver(serde=_serde())


def postgres_checkpointer(pool):
    """STORAGE=postgres：状态存进 Postgres，服务重启后仍能继续人工确认。表由 xhs-db init 创建。"""
    from langgraph.checkpoint.postgres import PostgresSaver

    return PostgresSaver(pool, serde=_serde())


def build_graph(deps: Deps, checkpointer=None):
    n = make_nodes(deps)
    g = StateGraph(AgentState)
    for name, fn in n.items():
        g.add_node(name, fn)
    g.add_node("human_gate", lambda s: {})  # 暂停点，本身不做事

    g.set_entry_point("retrieve")
    g.add_edge("retrieve", "plan")
    # plan / write 调模型失败（格式重试用尽、网络错误）时 status=error，直接结束
    def ok_or_end(next_node: str):
        return lambda state: END if state.get("status") == "error" else next_node

    g.add_conditional_edges("plan", ok_or_end("write"), ["write", END])
    g.add_conditional_edges("write", ok_or_end("review"), ["review", END])

    def after_review(state: AgentState) -> str:
        if state["review"].passed:
            return "cover"
        # attempts 含首次，所以总共最多 1 + max_review_retries 次
        if state["attempts"] <= deps.settings.max_review_retries:
            return "write"
        return "give_up"

    g.add_conditional_edges("review", after_review, ["cover", "write", "give_up"])
    g.add_edge("cover", "human_gate")

    def after_human(state: AgentState) -> str:
        return "write" if state.get("human_decision") == "revise" else "finalize"

    g.add_conditional_edges("human_gate", after_human, ["write", "finalize"])
    g.add_edge("give_up", END)
    g.add_edge("finalize", END)

    return g.compile(checkpointer=checkpointer or default_checkpointer(), interrupt_before=["human_gate"])


class XhsAgent:
    """对外的简单封装：start() 跑到人工确认点，resume() 传入人工决定继续。"""

    def __init__(
        self,
        settings: Settings | None = None,
        llm: LLM | None = None,
        retriever: Retriever | None = None,
    ):
        self.settings = s = settings or Settings.from_env()
        use_pg = s.storage == "postgres" or (s.retriever == "pgvector" and retriever is None)
        self.pool = None
        if use_pg:
            from .db import make_pool

            self.pool = make_pool(s)
        deps = Deps(
            settings=s,
            llm=llm or build_llm(s),
            retriever=retriever or build_retriever(s, self.pool),
            tracer=Tracer(s.log_file),
            sensitive_words=load_words(s.sensitive_words),
            merchant_words=load_words(s.merchant_words),
        )
        self.deps = deps
        self.tracer = deps.tracer
        self.run_store = None
        checkpointer = None
        self.events: EventStore = JsonlEventStore(s.events_file)
        self.shops: ShopStore = JsonShopStore(s.shops_file)
        if s.storage == "postgres":
            from .db import RunStore

            checkpointer = postgres_checkpointer(self.pool)
            self.run_store = RunStore(self.pool)
            self.events = PgEventStore(self.pool)
            self.shops = PgShopStore(self.pool)
        self.graph = build_graph(deps, checkpointer)

    def _record(self, state: AgentState) -> AgentState:
        """运行记录写进 runs 表；写失败只记日志，不影响本次生成。"""
        if self.run_store and state.get("run_id"):
            try:
                self.run_store.save(state)
            except Exception as e:  # noqa: BLE001
                self.tracer.log(run_id=state["run_id"], node="run_store", ok=False, error=f"{type(e).__name__}: {e}")
        return state

    def close(self) -> None:
        if self.pool is not None:
            self.pool.close()

    def _config(self, run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def ask(self, request: NoteRequest) -> list[str]:
        """素材追问：生成要问店主的问题（商家模式）。把回答放进 request.answers 再调用 start()。"""
        return ask_questions(self.deps, "ask-" + uuid.uuid4().hex[:8], request)

    def _stream(self, graph_input, run_id: str) -> Iterator[tuple[str, object]]:
        """逐个产出 ("node", 节点名)，最后产出 ("done", 最终状态)。Web 端据此显示真实进度。"""
        cfg = self._config(run_id)
        for chunk in self.graph.stream(graph_input, cfg, stream_mode="updates"):
            for node in chunk:
                if not node.startswith("__"):
                    yield "node", node
        yield "done", self._record(self.get(run_id))

    def start_stream(self, request: NoteRequest) -> Iterator[tuple[str, object]]:
        run_id = uuid.uuid4().hex[:12]
        self.tracer.log(run_id=run_id, node="start", request=request.model_dump())
        return self._stream({"run_id": run_id, "request": request}, run_id)

    def resume_stream(self, run_id: str, decision: str, feedback: str = "") -> Iterator[tuple[str, object]]:
        if decision not in ("approve", "reject", "revise"):
            raise ValueError(f"未知决定：{decision}")
        cfg = self._config(run_id)
        if not self.graph.get_state(cfg).next:
            raise ValueError(f"运行 {run_id} 不在等待确认状态")
        self.graph.update_state(cfg, {"human_decision": decision, "human_feedback": feedback})
        return self._stream(None, run_id)

    def start(self, request: NoteRequest) -> AgentState:
        return _last(self.start_stream(request))

    def resume(self, run_id: str, decision: str, feedback: str = "") -> AgentState:
        return _last(self.resume_stream(run_id, decision, feedback))

    def record_event(self, run_id: str, event: EventIn) -> Event:
        """记下店主的反馈 / 使用行为，顺带存上店名和内容类型方便按店汇总。"""
        state = self.get(run_id)
        if not state:
            raise ValueError(f"运行 {run_id} 不存在")
        req = state["request"]
        e = Event(
            **event.model_dump(), run_id=run_id, shop_code=req.shop_code,
            shop_name=req.shop.name if req.shop else "", content_type=req.content_type or "",
        )
        self.events.add(e)
        return e

    def get(self, run_id: str) -> AgentState:
        return self.graph.get_state(self._config(run_id)).values

    def mark_published(self, run_id: str, post_url: str | None) -> AgentState:
        """发布成功后记一下，防止同一篇被重复发布。"""
        self.graph.update_state(self._config(run_id), {"published": True, "publish_url": post_url or ""})
        return self.get(run_id)


def _last(events: Iterator[tuple[str, object]]) -> AgentState:
    state = None
    for kind, value in events:
        if kind == "done":
            state = value
    return state
