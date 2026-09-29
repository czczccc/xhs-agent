"""各节点之间传递的结构化数据。LLM 输出都先过 Pydantic 校验，校验失败即视为节点失败。"""

from __future__ import annotations

from typing import Literal, TypedDict

from pydantic import BaseModel, Field


class ShopProfile(BaseModel):
    """店铺档案：商家填一次，每次生成都会用到。这里写的是笔记里允许出现的「事实」。"""

    name: str = Field(..., description="店名")
    shop_type: str = Field(..., description="细分品类，例如：火锅、咖啡、家常菜")
    city: str = ""
    area: str = Field("", description="位置/商圈，例如：西湖区文三路")
    avg_price: int | None = Field(None, description="人均（元）")
    signature: list[str] = Field(default_factory=list, description="招牌菜/主推产品")
    highlights: list[str] = Field(default_factory=list, description="卖点：环境、食材、服务等")
    hours: str = ""
    tone: str = Field("亲切真诚", description="账号口吻")

    def facts(self) -> str:
        lines = [f"店名：{self.name}", f"品类：{self.shop_type}"]
        if self.city or self.area:
            lines.append(f"位置：{self.city}{self.area}")
        if self.avg_price:
            lines.append(f"人均：{self.avg_price} 元")
        if self.signature:
            lines.append("招牌：" + "、".join(self.signature))
        if self.highlights:
            lines.append("卖点：" + "、".join(self.highlights))
        if self.hours:
            lines.append(f"营业时间：{self.hours}")
        return "\n".join(lines)


ContentType = Literal["上新", "活动", "日常", "老板故事", "节日", "店铺介绍"]


class Answer(BaseModel):
    """素材追问：动笔前问店主的问题和店主的回答。回答里的细节算作可以写进笔记的事实。"""

    question: str
    answer: str


class Questions(BaseModel):
    questions: list[str] = Field(..., min_length=1, max_length=3)


class NoteRequest(BaseModel):
    topic: str = Field(..., description="选题；商家模式下是本次要发的内容，例如：周三酸菜鱼半价")
    audience: str = "20-30 岁城市白领"
    style: Literal["干货", "种草", "日常分享", "测评"] = "干货"
    extra: str = ""
    # 商家模式：带上店铺档案后，以店铺官方账号身份写，并校验不编造价格/优惠
    shop: ShopProfile | None = None
    content_type: ContentType | None = None
    answers: list[Answer] = Field(default_factory=list)
    photos: list[str] = Field(default_factory=list, max_length=9, description="已上传照片的 id，第一张做封面")
    cover_style: Literal["big", "bottom", "badge"] = "big"
    shop_code: str | None = None  # 试用码，由服务端按请求头填，前端传的会被覆盖

    def details(self) -> str:
        """店主对追问的回答（空回答不算）。"""
        return "\n".join(f"问：{a.question}\n答：{a.answer.strip()}" for a in self.answers if a.answer.strip())

    def facts(self) -> str:
        """商家模式下笔记里允许出现的全部事实：店铺档案 + 本次需求 + 店主回答。"""
        if not self.shop:
            return ""
        parts = [self.shop.facts(), f"本次内容：{self.topic}"]
        if self.extra:
            parts.append(f"补充信息：{self.extra}")
        if self.details():
            parts.append("店主补充：\n" + self.details())
        return "\n".join(parts)


class ReferenceNote(BaseModel):
    title: str
    body: str
    tags: list[str] = []
    likes: int = 0
    category: str | None = None
    collects: int | None = None
    comments: int | None = None
    shop_type: str | None = None
    content_type: str | None = None
    author_type: str | None = None  # 商家 / 博主 / 用户
    city: str | None = None
    score: float = 0.0


class ContentPlan(BaseModel):
    """规划节点输出：先定结构再写正文，便于审核和人工修改。"""

    angle: str = Field(..., description="切入角度")
    title_candidates: list[str] = Field(..., min_length=3, max_length=5)
    outline: list[str] = Field(..., min_length=3)
    tags: list[str] = Field(..., min_length=3, max_length=10)
    cover_text: str = Field(..., max_length=20, description="封面大字")


class Draft(BaseModel):
    title: str = Field(..., max_length=20)  # 小红书标题上限 20 字
    body: str
    tags: list[str]


class ReviewResult(BaseModel):
    passed: bool
    issues: list[str] = []


class AgentState(TypedDict, total=False):
    run_id: str
    request: NoteRequest
    references: list[ReferenceNote]
    plan: ContentPlan
    draft: Draft
    cover_path: str
    review: ReviewResult
    attempts: int
    human_decision: Literal["approve", "reject", "revise"]
    human_feedback: str
    status: str
    error: str
    published: bool  # 是否已经发布到小红书；防止重复发
    publish_url: str
