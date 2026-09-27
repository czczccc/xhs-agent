import json

import pytest

from xhs_agent.graph import XhsAgent
from xhs_agent.llm import FakeLLM, LLMResult
from xhs_agent.schemas import NoteRequest


def test_happy_path_pauses_for_human_then_approves(agent, settings):
    state = agent.start(NoteRequest(topic="打工人一周带饭便当"))
    assert state["status"] == "pending_review"
    assert state["review"].passed
    assert len(state["references"]) == 3
    assert "带饭" in state["references"][0].title
    assert (settings.output_dir / f"{state['run_id']}_cover_big.png").exists()

    final = agent.resume(state["run_id"], "approve")
    assert final["status"] == "approved"


def test_reject(agent):
    state = agent.start(NoteRequest(topic="新手健身"))
    assert agent.resume(state["run_id"], "reject")["status"] == "rejected"


def test_revise_rewrites_and_pauses_again(agent, settings):
    state = agent.start(NoteRequest(topic="新手健身"))
    again = agent.resume(state["run_id"], "revise", "语气更活泼一点")
    assert again["status"] == "pending_review"
    assert again["attempts"] == 2
    logs = [json.loads(l) for l in settings.log_file.read_text(encoding="utf-8").splitlines()]
    assert sum(1 for r in logs if r["run_id"] == state["run_id"] and r["node"] == "write") == 2


def test_resume_unknown_decision(agent):
    state = agent.start(NoteRequest(topic="新手健身"))
    with pytest.raises(ValueError):
        agent.resume(state["run_id"], "maybe")


class BannedWordLLM(FakeLLM):
    """正文里总是带违规词，用来测试重试和放弃。"""

    def complete(self, system, user, json_mode=False):
        res = super().complete(system, user, json_mode)
        if "[task:write]" in system:
            data = json.loads(res.text)
            data["body"] = "全网最低价！" + data["body"]
            return LLMResult(json.dumps(data, ensure_ascii=False), res.prompt_tokens, res.completion_tokens)
        return res


def test_review_retries_then_gives_up(settings):
    settings.max_review_retries = 2
    agent = XhsAgent(settings=settings, llm=BannedWordLLM())
    state = agent.start(NoteRequest(topic="租房改造"))
    assert state["status"] == "failed"
    assert state["attempts"] == 3
    assert any("全网最低" in i for i in state["review"].issues)


class FixOnRetryLLM(BannedWordLLM):
    """第一次写违规，收到审核意见后改正。"""

    def complete(self, system, user, json_mode=False):
        if "[task:write]" in system and "审核未通过" in user:
            return FakeLLM.complete(self, system, user, json_mode)
        return super().complete(system, user, json_mode)


def test_review_feedback_fixes_draft(settings):
    agent = XhsAgent(settings=settings, llm=FixOnRetryLLM())
    state = agent.start(NoteRequest(topic="租房改造"))
    assert state["status"] == "pending_review"
    assert state["attempts"] == 2


class BrokenJsonLLM(FakeLLM):
    def complete(self, system, user, json_mode=False):
        return LLMResult("这不是 JSON")


def test_bad_llm_output_ends_run_with_error(settings):
    settings.max_format_retries = 1
    agent = XhsAgent(settings=settings, llm=BrokenJsonLLM())
    state = agent.start(NoteRequest(topic="租房改造"))
    assert state["status"] == "error"
    assert "ContentPlan" in state["error"] and "这不是 JSON" in state["error"]
    assert "draft" not in state
    rec = [json.loads(l) for l in settings.log_file.read_text(encoding="utf-8").splitlines()][-1]
    assert rec["node"] == "plan" and rec["ok"] is False and rec["format_retries"] == 1


class FlakyWriteLLM(FakeLLM):
    """write 前 bad_times 次输出缺字段的 JSON，之后输出合法 JSON 但后面多一句话。"""

    def __init__(self, bad_times: int):
        self.bad_left = bad_times
        self.write_prompts: list[str] = []

    def complete(self, system, user, json_mode=False):
        res = super().complete(system, user, json_mode)
        if "[task:write]" not in system:
            return res
        self.write_prompts.append(user)
        if self.bad_left > 0:
            self.bad_left -= 1
            return LLMResult('{"title": "只有标题"}', 10, 5)  # 缺 body/tags，校验失败
        return LLMResult(res.text + "\n以上就是笔记内容～", res.prompt_tokens, res.completion_tokens)


def test_format_error_is_retried_with_feedback(settings):
    settings.max_format_retries = 1
    llm = FlakyWriteLLM(bad_times=1)
    state = XhsAgent(settings=settings, llm=llm).start(NoteRequest(topic="租房改造"))
    assert state["status"] == "pending_review"  # JSON 后面多出的文字也能容忍
    assert len(llm.write_prompts) == 2
    assert "上一次输出无法使用" in llm.write_prompts[1]
    logs = [json.loads(l) for l in settings.log_file.read_text(encoding="utf-8").splitlines()]
    write = [r for r in logs if r["node"] == "write"]
    assert len(write) == 1 and write[0]["ok"] and write[0]["format_retries"] == 1


def test_format_retries_exhausted_on_write(settings):
    settings.max_format_retries = 1
    state = XhsAgent(settings=settings, llm=FlakyWriteLLM(bad_times=2)).start(NoteRequest(topic="租房改造"))
    assert state["status"] == "error" and "Draft" in state["error"]
    assert state["plan"] is not None


SHOP = {
    "name": "巷口小馆", "shop_type": "家常菜", "city": "杭州", "area": "文三路",
    "avg_price": 68, "signature": ["酸菜鱼", "小炒黄牛肉"], "hours": "11:00-22:00",
}


class InventPriceOnceLLM(FakeLLM):
    """商家模式下第一次写作编造折扣，收到审核意见后改正；同时记录 prompt。"""

    def __init__(self):
        self.prompts: list[tuple[str, str]] = []

    def complete(self, system, user, json_mode=False):
        self.prompts.append((system, user))
        res = super().complete(system, user, json_mode)
        if "[task:write]" in system and "审核未通过" not in user:
            data = json.loads(res.text)
            data["body"] = "全场8.8折，" + data["body"]
            return LLMResult(json.dumps(data, ensure_ascii=False), res.prompt_tokens, res.completion_tokens)
        return res


def test_merchant_mode_uses_shop_facts_and_blocks_invented_discount(settings):
    llm = InventPriceOnceLLM()
    req = NoteRequest(topic="周三酸菜鱼半价", content_type="活动", shop=SHOP)
    state = XhsAgent(settings=settings, llm=llm).start(req)
    assert state["status"] == "pending_review" and state["attempts"] == 2
    plan_system, plan_user = llm.prompts[0]
    assert "店铺官方账号" in plan_system and "巷口小馆" in plan_user and "人均：68 元" in plan_user
    rewrite_user = [u for s, u in llm.prompts if "[task:write]" in s][1]
    assert "8.8折" in rewrite_user  # 审核意见带回给了模型


def test_merchant_words_only_apply_in_merchant_mode(settings):
    class HealthClaimLLM(FakeLLM):
        def complete(self, system, user, json_mode=False):
            res = super().complete(system, user, json_mode)
            if "[task:write]" in system:
                data = json.loads(res.text)
                data["body"] = "吃了能减肥。" + data["body"]
                return LLMResult(json.dumps(data, ensure_ascii=False), res.prompt_tokens, res.completion_tokens)
            return res

    settings.max_review_retries = 0
    generic = XhsAgent(settings=settings, llm=HealthClaimLLM()).start(NoteRequest(topic="减脂餐"))
    assert generic["status"] == "pending_review"
    merchant = XhsAgent(settings=settings, llm=HealthClaimLLM()).start(NoteRequest(topic="轻食上新", shop=SHOP))
    assert merchant["status"] == "failed" and "减肥" in merchant["review"].issues[0]


def test_ask_questions_only_in_merchant_mode(agent, settings):
    assert agent.ask(NoteRequest(topic="减脂餐")) == []
    qs = agent.ask(NoteRequest(topic="秋天上新板栗烧鸡", content_type="上新", shop=SHOP))
    assert 1 <= len(qs) <= 3
    assert XhsAgent(settings=settings, llm=BrokenJsonLLM()).ask(NoteRequest(topic="x", shop=SHOP)) == []


def test_owner_answers_reach_prompts_and_count_as_facts(settings):
    class UsesAnswerLLM(FakeLLM):
        def __init__(self):
            self.users: list[str] = []

        def complete(self, system, user, json_mode=False):
            self.users.append(user)
            res = super().complete(system, user, json_mode)
            if "[task:write]" in system:
                data = json.loads(res.text)
                data["body"] = "板栗每天只剥30份，剥完就收。" + data["body"]
                return LLMResult(json.dumps(data, ensure_ascii=False), res.prompt_tokens, res.completion_tokens)
            return res

    from xhs_agent.schemas import Answer

    llm = UsesAnswerLLM()
    req = NoteRequest(
        topic="秋天上新板栗烧鸡", content_type="上新", shop=SHOP,
        answers=[Answer(question="板栗从哪来？", answer="老家亲戚寄的，每天只剥30份"), Answer(question="空的", answer=" ")],
    )
    state = XhsAgent(settings=settings, llm=llm).start(req)
    assert state["status"] == "pending_review" and state["attempts"] == 1  # 「30份」来自店主回答，不算编造
    assert all("老家亲戚寄的" in u for u in llm.users[:2]) and "空的" not in llm.users[0]

    no_answer = XhsAgent(settings=settings, llm=UsesAnswerLLM()).start(req.model_copy(update={"answers": []}))
    assert "30份" in " ".join(no_answer["review"].issues) or no_answer["attempts"] > 1
