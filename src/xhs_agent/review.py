"""规则审核：敏感词 / 广告法绝对化用语、标题长度、正文长度、标签数量；
商家模式下额外检查价格、优惠、时间是否都来自给定事实（防编造）。

后续可加一层 LLM 审核（roadmap M3），规则层先挡住确定性问题。
"""

from __future__ import annotations

import re
from pathlib import Path

from .schemas import Draft, ReviewResult

TITLE_MAX = 20
BODY_MIN, BODY_MAX = 100, 1000


def load_words(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        w.strip()
        for w in path.read_text(encoding="utf-8").splitlines()
        if w.strip() and not w.startswith("#")
    ]


# 带单位的数字：价格、折扣、份数、时间、日期。商家模式下这些必须能在给定事实里找到
_CLAIM_NUM = re.compile(r"[¥￥]\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|块|折|%|％|份|点|号|月|日|周年)")
# 优惠类说法：事实里没提到就不能写
PROMO_WORDS = ["半价", "买一送一", "免费", "赠送", "满减", "特价", "优惠券", "秒杀", "团购", "折扣", "打折"]


def unsupported_claims(text: str, facts: str) -> list[str]:
    """找出正文里有、但店铺信息/本次需求里没有的数字和优惠说法（大概率是模型编的）。"""
    known = set(re.findall(r"\d+(?:\.\d+)?", facts))
    known |= {str(int(float(x))) for x in known}  # 02:00 → 2
    for h in re.findall(r"(\d{1,2}):\d{2}", facts):  # 23:00 → 晚上 11 点
        if 12 < int(h) <= 24:
            known.add(str(int(h) - 12))
    found: list[str] = []
    for m in _CLAIM_NUM.finditer(text):
        if (m.group(1) or m.group(2)) not in known:
            found.append(m.group(0).strip())
    found += [w for w in PROMO_WORDS if w in text and w not in facts]
    return list(dict.fromkeys(found))


def check(draft: Draft, words: list[str], facts: str | None = None) -> ReviewResult:
    issues: list[str] = []
    text = draft.title + "\n" + draft.body
    # 词表里 ! 开头的是放行短语（如 !第一次），先从文本中去掉，避免被「第一」误判
    for allowed in (w[1:] for w in words if w.startswith("!")):
        text = text.replace(allowed, "")
    hits = sorted({w for w in words if not w.startswith("!") and w in text})
    if hits:
        issues.append(f"包含敏感词/违规用语：{'、'.join(hits)}")
    if len(draft.title) > TITLE_MAX:
        issues.append(f"标题 {len(draft.title)} 字，超过 {TITLE_MAX} 字")
    if not BODY_MIN <= len(draft.body) <= BODY_MAX:
        issues.append(f"正文 {len(draft.body)} 字，应在 {BODY_MIN}-{BODY_MAX} 字之间")
    if not 3 <= len(draft.tags) <= 10:
        issues.append(f"标签 {len(draft.tags)} 个，应为 3-10 个")
    if facts is not None:
        claims = unsupported_claims(draft.title + "\n" + draft.body, facts)
        if claims:
            issues.append(
                f"出现了店铺信息和本次需求里没有的内容：{'、'.join(claims)}。"
                "价格、优惠、时间只能用给定信息，请删掉或改成已给出的内容"
            )
    return ReviewResult(passed=not issues, issues=issues)
