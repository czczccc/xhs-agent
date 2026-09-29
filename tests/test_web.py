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


def test_web_publish_disabled_by_default(agent):
    web._agent = agent
    c = TestClient(web.app)
    r = c.post("/api/runs", json={"topic": "周末杭州一日游"}).json()
    assert r["publish_enabled"] is False and r["published"] is False
    assert c.get("/api/config").json()["publish_enabled"] is False
    assert c.post(f"/api/runs/{r['run_id']}/publish", json={"confirm": True}).status_code == 400


def test_web_publish_flow(settings, monkeypatch):
    from xhs_agent.graph import XhsAgent
    from xhs_agent.llm import FakeLLM
    from xhs_agent.publish import PublishError, PublishResult

    settings.xhs_mcp_url = "http://fake-xhs-mcp:18060/mcp"
    web._agent = XhsAgent(settings=settings, llm=FakeLLM())
    c = TestClient(web.app)
    run_id = c.post("/api/runs", json={"topic": "周末杭州一日游"}).json()["run_id"]

    # 没确认就不发
    assert c.post(f"/api/runs/{run_id}/publish", json={"confirm": False}).status_code == 400

    async def fake_publish_note(settings, *, title, content, tags, images):
        assert images[0].startswith("http") and "/cover" in images[0]
        return PublishResult(message="发布成功", post_url="https://www.xiaohongshu.com/explore/abc123")

    monkeypatch.setattr(web, "publish_note", fake_publish_note)
    res = c.post(f"/api/runs/{run_id}/publish", json={"confirm": True}).json()
    assert res == {"ok": True, "message": "发布成功", "post_url": "https://www.xiaohongshu.com/explore/abc123"}

    got = c.get(f"/api/runs/{run_id}").json()
    assert got["published"] is True
    assert got["publish_url"] == "https://www.xiaohongshu.com/explore/abc123"

    # 已发布过的不能再发
    assert c.post(f"/api/runs/{run_id}/publish", json={"confirm": True}).status_code == 409

    # 重写之后是新草稿，发布状态要清掉，能再次发布
    revised = c.post(f"/api/runs/{run_id}/decision", json={"decision": "revise", "feedback": "更活泼"}).json()
    assert revised["published"] is False and revised["publish_url"] is None

    async def failing_publish_note(settings, **kwargs):
        raise PublishError("cookie 过期了，去重新登录一下")

    monkeypatch.setattr(web, "publish_note", failing_publish_note)
    resp = c.post(f"/api/runs/{run_id}/publish", json={"confirm": True})
    assert resp.status_code == 502 and "cookie" in resp.json()["detail"]

    # 登录失效单独用 428：401 在前端会被当成试用码失效
    from xhs_agent.publish import LoginExpiredError

    async def expired_publish_note(settings, **kwargs):
        raise LoginExpiredError("小红书登录失效了")

    monkeypatch.setattr(web, "publish_note", expired_publish_note)
    resp = c.post(f"/api/runs/{run_id}/publish", json={"confirm": True})
    assert resp.status_code == 428 and "登录失效" in resp.json()["detail"]


def test_web_xhs_login_endpoints_disabled_by_default(agent):
    web._agent = agent
    c = TestClient(web.app)
    assert c.get("/api/xhs-login/status").status_code == 400
    assert c.post("/api/xhs-login/qrcode").status_code == 400
    assert c.post("/api/xhs-login/logout").status_code == 400


def test_web_xhs_login_flow(settings, monkeypatch):
    from xhs_agent.graph import XhsAgent
    from xhs_agent.llm import FakeLLM
    from xhs_agent.publish import LoginQrCode, LoginStatus, PublishError

    settings.xhs_mcp_url = "http://fake-xhs-mcp:18060/mcp"
    web._agent = XhsAgent(settings=settings, llm=FakeLLM())
    c = TestClient(web.app)

    async def not_logged_in(settings):
        return LoginStatus(logged_in=False, message="未登录")

    monkeypatch.setattr(web, "check_login_status", not_logged_in)
    assert c.get("/api/xhs-login/status").json() == {"logged_in": False, "message": "未登录"}

    async def fake_qrcode(settings):
        return LoginQrCode(image="data:image/png;base64,Zm9v", expires_in=120)

    monkeypatch.setattr(web, "get_login_qrcode", fake_qrcode)
    assert c.post("/api/xhs-login/qrcode").json() == {"image": "data:image/png;base64,Zm9v", "expires_in": 120, "already_logged_in": False}

    async def qrcode_fails(settings):
        raise PublishError("连不上发布服务")

    monkeypatch.setattr(web, "get_login_qrcode", qrcode_fails)
    resp = c.post("/api/xhs-login/qrcode")
    assert resp.status_code == 502 and "连不上" in resp.json()["detail"]

    async def fake_logout(settings):
        return None

    monkeypatch.setattr(web, "xhs_logout", fake_logout)
    assert c.post("/api/xhs-login/logout").json() == {"ok": True}


def test_web_xhs_phone_login(settings, monkeypatch):
    from xhs_agent.graph import XhsAgent
    from xhs_agent.llm import FakeLLM
    from xhs_agent.publish import LoginStep, PublishError

    settings.xhs_mcp_url = "http://fake-xhs-mcp:18060/mcp"
    web._agent = XhsAgent(settings=settings, llm=FakeLLM())
    c = TestClient(web.app)
    calls = []

    async def fake_send(settings, phone):
        calls.append(("send", phone))
        return LoginStep(message="验证码已发送", screenshot="data:image/png;base64,Zm9v")

    async def fake_verify(settings, code):
        calls.append(("verify", code))
        return LoginStep(message="creator 登录成功")

    monkeypatch.setattr(web, "send_login_code", fake_send)
    monkeypatch.setattr(web, "verify_login_code", fake_verify)
    assert c.post("/api/xhs-login/phone", json={"phone": "13800000000"}).json() == {
        "ok": True, "message": "验证码已发送", "screenshot": "data:image/png;base64,Zm9v"}
    assert c.post("/api/xhs-login/verify", json={"code": "123456"}).json() == {"ok": True, "message": "creator 登录成功", "screenshot": None}
    assert calls == [("send", "13800000000"), ("verify", "123456")]

    async def verify_fails(settings, code):
        raise PublishError("验证码不对")

    monkeypatch.setattr(web, "verify_login_code", verify_fails)
    resp = c.post("/api/xhs-login/verify", json={"code": "000000"})
    assert resp.status_code == 502 and "验证码不对" in resp.json()["detail"]
