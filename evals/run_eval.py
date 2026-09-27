"""离线评测：对 topics.jsonl 里的选题批量生成，按规则打分，可选 LLM 裁判打分。

用法：
    python evals/run_eval.py                 # 规则打分
    python evals/run_eval.py --judge         # 追加 LLM 裁判（需要配置 LLM_API_KEY）
    python evals/run_eval.py --limit 5       # 只跑前 5 个
    python evals/run_eval.py --topics evals/merchant_topics.jsonl --judge   # 餐饮商家用例
    python evals/run_eval.py --topics evals/merchant_topics.jsonl --judge --interview   # 带素材追问

结果写到 evals/results/<时间戳>/：每条明细 details.jsonl + 汇总 summary.json。
用 FakeLLM 跑出来的分数只能证明流程能跑通，不代表真实效果；简历里的数字请用真实模型跑的结果。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from xhs_agent.graph import XhsAgent  # noqa: E402
from xhs_agent.llm import parse_json  # noqa: E402
from xhs_agent.review import unsupported_claims  # noqa: E402
from xhs_agent.schemas import Answer, NoteRequest  # noqa: E402
from xhs_agent.tracing import summarize  # noqa: E402

JUDGE_SYSTEM = """你是小红书内容运营专家。按下面维度给笔记打 1-5 分，输出 JSON：
{"hook": 标题和开头吸引力, "relevance": 是否紧扣选题, "usefulness": 信息量和可操作性,
 "tone": 是否像真人博主口吻, "comment": 一句话点评}
只输出 JSON。"""

MERCHANT_JUDGE_SYSTEM = """你是挑剔的餐饮品牌小红书运营总监，给下面这篇店铺官方笔记打 1-5 分，输出 JSON。
评分要严格：5 分 = 可以直接发且明显优于同行；4 分 = 小改就能发；3 分 = 及格但平庸、模板感重；2 分及以下 = 有明显问题。
{"hook": 标题和开头能否让刷到的人停下来, "relevance": 是否紧扣本次内容并用上店铺信息,
 "appeal": 看完想不想去这家店, "tone": 是否像真实店主在说话而不是 AI 模板, "comment": 一句话点评}
只输出 JSON。"""
MERCHANT_DIMS = ["hook", "relevance", "appeal", "tone"]

# --interview：模拟店主回答 Agent 的追问。店主只知道用例里的 owner_notes，没提到的就如实说没有
OWNER_SYSTEM = """你在扮演一家店的店主，正在回答运营的提问。下面【真实情况】是你知道的全部事实。
- 只根据真实情况回答，用店主口语，一两句话。
- 问题里只有一部分能在真实情况里找到答案的，只回答这部分，其余不提，绝不编造。
- 完全答不上来的，这一条就只输出「（无）」。
输出 JSON：{"answers": ["对问题1的回答", "对问题2的回答", ...]}，顺序与问题一致，只输出 JSON。"""
REQUEST_FIELDS = ("topic", "style", "extra", "audience", "content_type", "shop")

INTERACTION = re.compile(r"(评论区|留言|你们|大家|欢迎|一起|？|\?)")


def merchant_scores(state: dict) -> dict:
    """商家用例：提到店名或招牌；没有编造店铺信息/本次需求之外的价格、优惠、时间。"""
    d, req = state["draft"], state["request"]
    text = d.title + "\n" + d.body
    invented = unsupported_claims(text, req.facts())
    return {
        "shop_mentioned": int(req.shop.name in text or any(s in text for s in req.shop.signature)),
        "no_fabrication": int(not invented),
        "invented": invented,
    }


def rule_scores(topic: str, state: dict) -> dict:
    d = state.get("draft")
    if d is None:
        return {"generated": 0}
    extra = merchant_scores(state) if state["request"].shop else {}
    # 选题的重叠二元组（「带饭便当」→ 带饭/饭便/便当）有 30% 以上出现在标题或正文里，算紧扣选题。
    # 字面匹配只是粗筛（改写成近义词会漏判），相关性以 --judge 的 relevance 为准。
    chars = "".join(re.findall(r"[一-龥A-Za-z0-9]", topic))
    grams = {chars[i : i + 2] for i in range(len(chars) - 1)}
    coverage = sum(g in d.title + d.body for g in grams) / (len(grams) or 1)
    return {
        "generated": 1,
        "review_passed": int(bool(state.get("review") and state["review"].passed)),
        "first_try_pass": int(state.get("attempts", 0) == 1 and state["review"].passed),
        "title_ok": int(len(d.title) <= 20),
        "tags_ok": int(3 <= len(d.tags) <= 10),
        "on_topic": int(coverage >= 0.3),
        "topic_coverage": round(coverage, 2),
        "has_interaction": int(bool(INTERACTION.search(d.body[-60:]))),
        **extra,
    }


def judge(agent: XhsAgent, topic: str, state: dict) -> dict:
    d = state["draft"]
    llm = agent_llm(agent)
    req = state["request"]
    if req.shop:
        user = f"【店铺信息和本次内容】\n{req.facts()}\n\n标题：{d.title}\n正文：{d.body}"
        out = llm.complete(MERCHANT_JUDGE_SYSTEM, user, json_mode=True)
    else:
        out = llm.complete(JUDGE_SYSTEM, f"选题：{topic}\n标题：{d.title}\n正文：{d.body}", json_mode=True)
    try:
        return parse_json(out.text)
    except Exception as e:  # 裁判输出坏了不影响其他结果
        return {"judge_error": str(e)}


def interview(agent: XhsAgent, req: NoteRequest, owner_notes: str) -> list[Answer]:
    """Agent 提问 → 模拟店主按 owner_notes 回答。模拟失败就当店主没回答。"""
    questions = agent.ask(req)
    if not questions:
        return []
    user = f"【真实情况】\n{owner_notes}\n\n【问题】\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    try:
        answers = parse_json(agent_llm(agent).complete(OWNER_SYSTEM, user, json_mode=True).text)["answers"]
    except Exception:  # noqa: BLE001
        return []
    return [Answer(question=q, answer=str(a)) for q, a in zip(questions, answers) if str(a).strip() not in ("", "（无）", "(无)")]


def agent_llm(agent: XhsAgent):
    from xhs_agent.llm import build_llm

    return build_llm(agent.settings)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topics", default=str(ROOT / "evals" / "topics.jsonl"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--interview", action="store_true", help="商家用例：先追问，再由模拟店主按 owner_notes 回答")
    args = ap.parse_args()

    topics = [json.loads(l) for l in Path(args.topics).read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        topics = topics[: args.limit]

    agent = XhsAgent()
    out_dir = ROOT / "evals" / "results" / datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for t in topics:
        start = time.perf_counter()
        try:
            req = NoteRequest(**{k: t[k] for k in REQUEST_FIELDS if t.get(k)})
            if args.interview and req.shop and t.get("owner_notes"):
                req.answers = interview(agent, req, t["owner_notes"])
            state = agent.start(req)
            error = state.get("error")
        except Exception as e:
            state, error = {}, f"{type(e).__name__}: {e}"
        row = {**t, "error": error, "wall_ms": round((time.perf_counter() - start) * 1000, 1)}
        row.update(rule_scores(t["topic"], state))
        if state.get("run_id"):
            row.update({k: v for k, v in summarize(agent.settings.log_file, state["run_id"]).items() if k != "steps"})
            row["attempts"] = state.get("attempts")
        if state.get("draft"):
            row["draft"] = state["draft"].model_dump()  # 留存正文，方便人工抽查
            if state["request"].answers:
                row["answers"] = [a.model_dump() for a in state["request"].answers]
        if args.judge and state.get("draft"):
            row["judge"] = judge(agent, t["topic"], state)
        rows.append(row)
        print(f"{t['id']} {t['topic']:<16} passed={row.get('review_passed')} attempts={row.get('attempts')}")

    with (out_dir / "details.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    n = len(rows)
    merchant = any(t.get("shop") for t in topics)
    metric_keys = ["generated", "review_passed", "first_try_pass", "title_ok", "tags_ok", "on_topic", "has_interaction"]
    if merchant:
        # 商家笔记刻意不用「评论区见」式结尾，has_interaction 不适用
        metric_keys = [k for k in metric_keys if k != "has_interaction"] + ["shop_mentioned", "no_fabrication"]
    summary = {
        "model": agent_llm(agent).model,
        "n": n,
        "interview": args.interview,
        "avg_answers": round(sum(len(r.get("answers", [])) for r in rows) / n, 2),
        "rates": {k: round(sum(r.get(k, 0) for r in rows) / n, 3) for k in metric_keys},
        "avg_latency_ms": round(sum(r.get("latency_ms", 0) for r in rows) / n, 1),
        "avg_tokens": round(sum(r.get("prompt_tokens", 0) + r.get("completion_tokens", 0) for r in rows) / n, 1),
    }
    if args.judge:
        dims = MERCHANT_DIMS if merchant else ["hook", "relevance", "usefulness", "tone"]
        # 裁判偶尔返回嵌套结构或缺字段，只统计各维度都是数字的
        def numeric(j):
            try:
                return all(isinstance(j[d], (int, float)) or str(j[d]).replace(".", "", 1).isdigit() for d in dims)
            except (KeyError, TypeError):
                return False

        judged = [r["judge"] for r in rows if isinstance(r.get("judge"), dict) and numeric(r["judge"])]
        summary["judged"] = len(judged)
        if judged:
            summary["judge_avg"] = {d: round(sum(float(j[d]) for j in judged) / len(judged), 2) for d in dims}
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"结果：{out_dir}")


if __name__ == "__main__":
    main()
