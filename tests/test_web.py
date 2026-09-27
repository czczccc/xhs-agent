from fastapi.testclient import TestClient

from xhs_agent.web import app as web


def test_web_flow(agent):
    web._agent = agent
    c = TestClient(web.app)
    assert "小红书" in c.get("/").text
    r = c.post("/api/runs", json={"topic": "周末杭州一日游", "style": "日常分享"}).json()
    assert r["status"] == "pending_review"
    assert c.get(r["cover_url"]).status_code == 200
    done = c.post(f"/api/runs/{r['run_id']}/decision", json={"decision": "approve"}).json()
    assert done["status"] == "approved"
    assert c.post(f"/api/runs/{r['run_id']}/decision", json={"decision": "approve"}).status_code == 400


def test_web_shows_llm_error(settings):
    from xhs_agent.graph import XhsAgent
    from xhs_agent.llm import FakeLLM, LLMResult

    class Broken(FakeLLM):
        def complete(self, system, user, json_mode=False):
            return LLMResult("oops")

    web._agent = XhsAgent(settings=settings, llm=Broken())
    r = TestClient(web.app).post("/api/runs", json={"topic": "周末杭州一日游"})
    assert r.status_code == 200
    assert r.json()["status"] == "error" and r.json()["error"]


def test_web_merchant_request(agent):
    web._agent = agent
    shop = {"name": "巷口小馆", "shop_type": "家常菜", "avg_price": 68, "signature": ["酸菜鱼"]}
    r = TestClient(web.app).post(
        "/api/runs", json={"topic": "周三酸菜鱼半价", "content_type": "活动", "shop": shop, "extra": "每周三全天"}
    )
    assert r.status_code == 200 and r.json()["status"] == "pending_review"
    assert agent.get(r.json()["run_id"])["request"].shop.name == "巷口小馆"


def test_web_questions(agent):
    web._agent = agent
    c = TestClient(web.app)
    shop = {"name": "巷口小馆", "shop_type": "家常菜"}
    assert len(c.post("/api/questions", json={"topic": "板栗烧鸡上新", "shop": shop}).json()["questions"]) >= 1
    assert c.post("/api/questions", json={"topic": "减脂餐"}).json()["questions"] == []
    r = c.post("/api/runs", json={"topic": "板栗烧鸡上新", "shop": shop,
                                  "answers": [{"question": "板栗从哪来？", "answer": "老家寄的"}]}).json()
    assert agent.get(r["run_id"])["request"].answers[0].answer == "老家寄的"


def _events(resp):
    import json as _json

    return [_json.loads(l[6:]) for l in resp.text.splitlines() if l.startswith("data: ")]


def test_web_stream_reports_real_progress(agent):
    web._agent = agent
    c = TestClient(web.app)
    ev = _events(c.post("/api/runs/stream", json={"topic": "周末杭州一日游"}))
    nodes = [e["node"] for e in ev if e["type"] == "node"]
    assert nodes == ["retrieve", "plan", "write", "review", "cover"]
    assert ev[-1]["type"] == "done" and ev[-1]["run"]["status"] == "pending_review"
    run_id = ev[-1]["run"]["run_id"]

    ev2 = _events(c.post(f"/api/runs/{run_id}/decision/stream", json={"decision": "revise", "feedback": "更口语"}))
    assert [e["node"] for e in ev2 if e["type"] == "node"][:2] == ["human_gate", "write"]
    assert ev2[-1]["run"]["status"] == "pending_review"
    assert c.post("/api/runs/nope/decision/stream", json={"decision": "approve"}).status_code == 404
