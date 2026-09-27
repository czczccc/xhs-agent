import json

from xhs_agent.llm import parse_json
from xhs_agent.review import check
from xhs_agent.schemas import Draft
from xhs_agent.tracing import Tracer, summarize

BODY = "正文" * 60


def test_parse_json_in_code_fence():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('{"a": 2}') == {"a": 2}


def test_review_flags_banned_words_and_lengths():
    d = Draft(title="这是全网最好用的方法", body="太短", tags=["a"])
    r = check(d, ["最好", "加微信"])
    assert not r.passed
    assert len(r.issues) == 3


def test_parse_json_ignores_trailing_text():
    assert parse_json('{"a": 1}\n以上就是笔记～') == {"a": 1}
    assert parse_json('好的：{"a": "x}"} 完') == {"a": "x}"}


def test_review_passes_clean_draft():
    assert check(Draft(title="好标题", body=BODY, tags=["a", "b", "c"]), ["最好"]).passed


def test_review_allowlist_phrases():
    words = ["第一", "!第一步"]
    tags = ["a", "b", "c"]
    assert check(Draft(title="好标题", body="第一步先备菜" + BODY, tags=tags), words).passed
    assert not check(Draft(title="好标题", body="第一步之后，销量第一" + BODY, tags=tags), words).passed


def test_tracer_summarize(tmp_path):
    t = Tracer(tmp_path / "log.jsonl")
    with t.step("r1", "plan") as rec:
        rec["prompt_tokens"] = 10
        rec["completion_tokens"] = 5
    with t.step("r2", "plan") as rec:
        rec["prompt_tokens"] = 99
    s = summarize(tmp_path / "log.jsonl", "r1")
    assert s["steps"] == 1 and s["prompt_tokens"] == 10 and s["completion_tokens"] == 5


def test_embedder_batches_and_orders_by_index():
    import httpx

    from xhs_agent.embedding import OpenAICompatEmbedder

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        data = [{"index": i, "embedding": [float(len(t))]} for i, t in enumerate(body["input"])]
        return httpx.Response(200, json={"data": list(reversed(data))})

    e = OpenAICompatEmbedder("http://x/v1", "k", "m", dimensions=8, batch_size=2, transport=httpx.MockTransport(handler))
    assert e.embed(["a", "bb", "ccc"]) == [[1.0], [2.0], [3.0]]
    assert [len(b["input"]) for b in seen] == [2, 1] and seen[0]["dimensions"] == 8


def test_parse_likes():
    from xhs_agent.db import parse_likes

    assert [parse_likes(v) for v in (12, "345", "1.2万", "3.5w", "2k", "1,024", "", None)] == [
        12, 345, 12000, 35000, 2000, 1024, 0, 0,
    ]


def test_load_notes_cleans_and_reports(tmp_path):
    from xhs_agent.db import load_notes

    f = tmp_path / "n.jsonl"
    rows = [
        {"title": "A", "body": "正文 #带饭[话题]# #便当[话题]#", "tags": ["带饭"], "likes": "1万"},
        {"title": "A", "body": "重复标题"},
        {"title": "", "body": "没标题"},
        {"title": "B", "body": "正文", "likes": "很多"},
    ]
    f.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n{坏json\n", encoding="utf-8")
    notes, problems = load_notes(f)
    assert [n.title for n in notes] == ["A"]
    assert notes[0].body == "正文" and notes[0].tags == ["带饭", "便当"] and notes[0].likes == 10000
    assert len(problems) == 4 and all(p.startswith("跳过") for p in problems)


def test_load_notes_json_array_and_fixes(tmp_path):
    from xhs_agent.db import load_notes

    long_title = "事情是这样的。老同学快十年没见了，" + "后来发生了很多事" * 10
    rows = [
        {"title": "只有标题", "body": "", "likes": 5},
        {"title": "被截断", "body": "列表页摘要到这里……"},
        {"title": long_title, "body": "摘要……"},
    ]
    f = tmp_path / "n.json"
    f.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    notes, problems = load_notes(f)
    assert [n.title for n in notes] == ["只有标题", "被截断", "事情是这样的"]
    assert notes[0].body == "" and notes[1].body == "列表页摘要到这里"
    assert notes[2].body == long_title
    assert problems == ["修正 第 3 条：标题过长，已移入正文，标题改为「事情是这样的」"]


FACTS = "店名：巷口小馆\n人均：68 元\n营业时间：11:00-22:00\n本次内容：周三酸菜鱼半价"


def test_unsupported_claims_flags_invented_prices_and_promos():
    from xhs_agent.review import unsupported_claims

    ok = "人均68元，酸菜鱼周三半价，11点开门"
    assert unsupported_claims(ok, FACTS) == []
    bad = "酸菜鱼只要¥39，全场8.8折，还免费送饮料，22点打烊"
    assert unsupported_claims(bad, FACTS) == ["¥39", "8.8折", "免费"]
    late = "营业时间：16:00-次日02:00；23:00 前到店"
    assert unsupported_claims("凌晨2点还在营业，晚上11点前来，下午4点开门", late) == []
    assert unsupported_claims("凌晨3点见", late) == ["3点"]


def test_review_uses_facts_only_in_merchant_mode():
    d = Draft(title="好标题", body="全场8折" + BODY, tags=["a", "b", "c"])
    assert check(d, []).passed  # 通用模式不查编造
    r = check(d, [], facts=FACTS)
    assert not r.passed and "8折" in r.issues[0]


def _chat_transport(stream_chunks=None, reject_non_stream=False):
    import httpx

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if not body.get("stream"):
            if reject_non_stream:
                return httpx.Response(400, json={"error": {"message": "Non-stream chat request is currently not supported"}})
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"a":<|ad|> 1}'}}],
                                             "usage": {"prompt_tokens": 5, "completion_tokens": 3}})
        lines = [f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}" for c in stream_chunks]
        lines.append('data: {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 4}}')
        return httpx.Response(200, text="\n\n".join(lines + ["data: [DONE]"]) + "\n\n",
                              headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def test_llm_non_stream_strips_control_tokens():
    from xhs_agent.llm import OpenAICompatLLM

    r = OpenAICompatLLM("http://x/v1", "k", "m", transport=_chat_transport()).complete("s", "u", json_mode=True)
    assert parse_json(r.text) == {"a": 1} and r.sanitized == 1 and (r.prompt_tokens, r.completion_tokens) == (5, 3)


def test_llm_stream_mode_assembles_chunks():
    import httpx
    import pytest

    from xhs_agent.llm import OpenAICompatLLM

    t = _chat_transport(['{"title": "板栗', "烧鸡<｜end▁of▁sentence｜>", '"}'], reject_non_stream=True)
    r = OpenAICompatLLM("http://x/v1", "k", "m", stream=True, transport=t).complete("s", "u")
    assert parse_json(r.text) == {"title": "板栗烧鸡"} and r.sanitized == 1
    assert (r.prompt_tokens, r.completion_tokens) == (7, 4)
    with pytest.raises(httpx.HTTPStatusError):  # 只支持流式的服务，非流式会 400
        OpenAICompatLLM("http://x/v1", "k", "m", transport=t).complete("s", "u")
