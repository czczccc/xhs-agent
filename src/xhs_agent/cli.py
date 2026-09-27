"""命令行：xhs-agent "选题" [--style 干货] [--yes]

商家模式：xhs-agent "周三酸菜鱼半价" --shop data/shops/example.json --type 活动 --extra "每周三全天"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .graph import XhsAgent
from .schemas import Answer, NoteRequest, ShopProfile
from .tracing import summarize


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="小红书图文生成 Agent")
    ap.add_argument("topic")
    ap.add_argument("--audience", default="20-30 岁城市白领")
    ap.add_argument("--style", default="干货", choices=["干货", "种草", "日常分享", "测评"])
    ap.add_argument("--yes", action="store_true", help="跳过人工确认，直接通过")
    ap.add_argument("--shop", type=Path, help="店铺档案 JSON（见 data/shops/example.json），给了就按店铺官方号来写")
    ap.add_argument("--type", dest="content_type", choices=["上新", "活动", "日常", "老板故事", "节日", "店铺介绍"])
    ap.add_argument("--extra", default="", help="补充信息：价格、活动时间、限量等")
    ap.add_argument("--no-ask", action="store_true", help="商家模式下跳过动笔前的素材追问")
    args = ap.parse_args(argv)

    shop = ShopProfile(**json.loads(args.shop.read_text(encoding="utf-8"))) if args.shop else None
    agent = XhsAgent()
    req = NoteRequest(
        topic=args.topic, audience=args.audience, style=args.style, extra=args.extra,
        shop=shop, content_type=args.content_type,
    )
    if shop and not (args.no_ask or args.yes):
        questions = agent.ask(req)
        if questions:
            print("动笔前想问你几句（用自己的话说一两句，越具体越好；直接回车跳过）：")
        for q in questions:
            a = input(f"\n{q}\n> ").strip()
            if a:
                req.answers.append(Answer(question=q, answer=a))
    state = agent.start(req)
    run_id = state["run_id"]

    while state.get("status") == "pending_review":
        d = state["draft"]
        print(f"\n=== {d.title} ===\n{d.body}\n" + " ".join("#" + t for t in d.tags))
        print(f"\n封面：{state['cover_path']}")
        if args.yes:
            state = agent.resume(run_id, "approve")
            break
        choice = input("\n[a]通过 / [r]驳回 / [e]修改意见 > ").strip().lower()
        if choice == "e":
            state = agent.resume(run_id, "revise", input("修改意见：").strip())
        else:
            state = agent.resume(run_id, "approve" if choice == "a" else "reject")

    if state.get("status") == "error":
        print("生成失败：", state.get("error"))
    if state.get("status") == "failed":
        print("审核多次未通过：", state["review"].issues)
    print(f"\n状态：{state.get('status')}  run_id={run_id}")
    print("统计：", json.dumps(summarize(agent.settings.log_file, run_id), ensure_ascii=False))


if __name__ == "__main__":
    main()
