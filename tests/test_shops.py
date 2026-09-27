"""试用码：访问控制、档案存服务端、每日额度、店与店之间隔离。"""

import pytest
from fastapi.testclient import TestClient

from xhs_agent.feedback import JsonlEventStore
from xhs_agent.graph import XhsAgent
from xhs_agent.llm import FakeLLM
from xhs_agent.shops import ALPHABET, JsonShopStore, new_code

PROFILE_A = {"name": "巷口小馆", "shop_type": "家常菜", "area": "文三路", "signature": ["酸菜鱼"]}
PROFILE_B = {"name": "山野咖啡", "shop_type": "咖啡"}


@pytest.fixture
def env(settings):
    from xhs_agent.web import app as web

    settings.require_shop_code = True
    web._agent = XhsAgent(settings=settings, llm=FakeLLM())
    shops = web._agent.shops
    a, b = shops.create("A 店", daily_limit=3), shops.create("B 店")
    return TestClient(web.app), a.code, b.code, settings


def h(code):
    return {"X-Shop-Code": code}


def test_code_format():
    codes = {new_code() for _ in range(200)}
    assert all(len(c) == 6 and set(c) <= set(ALPHABET) for c in codes)
    assert not set("01OIL") & set(ALPHABET)


def test_requires_valid_active_code(env):
    c, a, _, settings = env
    assert c.get("/api/shop").status_code == 401
    assert c.get("/api/shop", headers=h("ZZZZZZ")).status_code == 401
    assert c.get("/api/shop", headers=h(a.lower())).status_code == 200  # 不分大小写
    assert c.post("/api/runs", json={"topic": "x"}).status_code == 401
    JsonShopStore(settings.shops_file).update(a, active=False)
    r = c.get("/api/shop", headers=h(a))
    assert r.status_code == 401 and "停用" in r.json()["detail"]


def test_profile_lives_on_server_and_overrides_client(env):
    c, a, _, _ = env
    assert c.get("/api/shop", headers=h(a)).json()["profile"] is None
    assert c.post("/api/runs", json={"topic": "周三酸菜鱼半价"}, headers=h(a)).status_code == 409  # 先建档
    saved = c.put("/api/shop", json=PROFILE_A, headers=h(a)).json()
    assert saved["profile"]["name"] == "巷口小馆" and saved["code"] == a

    # 前端传了别的店名也没用，以服务端档案为准，并记上试用码
    run = c.post("/api/runs", json={"topic": "周三酸菜鱼半价", "shop": PROFILE_B}, headers=h(a)).json()
    from xhs_agent.web import app as web

    req = web._agent.get(run["run_id"])["request"]
    assert req.shop.name == "巷口小馆" and req.shop_code == a


def test_daily_limit_counts_runs_and_rewrites_not_questions(env):
    c, a, _, _ = env
    c.put("/api/shop", json=PROFILE_A, headers=h(a))
    for _ in range(5):
        assert c.post("/api/questions", json={"topic": "x"}, headers=h(a)).status_code == 200  # 追问不计
    r1 = c.post("/api/runs", json={"topic": "一"}, headers=h(a)).json()["run_id"]
    assert c.post(f"/api/runs/{r1}/decision", json={"decision": "revise", "feedback": "再短点"}, headers=h(a)).status_code == 200
    assert c.post("/api/runs/stream", json={"topic": "三"}, headers=h(a)).status_code == 200
    assert c.get("/api/shop", headers=h(a)).json()["used_today"] == 3
    over = c.post("/api/runs", json={"topic": "四"}, headers=h(a))
    assert over.status_code == 429 and "3 次" in over.json()["detail"]
    assert c.post(f"/api/runs/{r1}/decision/stream", json={"decision": "revise", "feedback": "x"}, headers=h(a)).status_code == 429


def test_shops_cannot_touch_each_others_runs(env):
    c, a, b, settings = env
    c.put("/api/shop", json=PROFILE_A, headers=h(a))
    c.put("/api/shop", json=PROFILE_B, headers=h(b))
    run = c.post("/api/runs", json={"topic": "周三酸菜鱼半价"}, headers=h(a)).json()["run_id"]
    for method, url, body in [
        ("get", f"/api/runs/{run}", None),
        ("post", f"/api/runs/{run}/decision", {"decision": "revise", "feedback": "x"}),
        ("post", f"/api/runs/{run}/events", {"kind": "copy_body"}),
    ]:
        assert getattr(c, method)(url, headers=h(b), **({"json": body} if body else {})).status_code == 404
    assert c.post(f"/api/runs/{run}/events", json={"kind": "rating", "rating": "ready"}, headers=h(a)).status_code == 200
    ev = JsonlEventStore(settings.events_file).all()
    assert [(e.shop_code, e.shop_name) for e in ev] == [(a, "巷口小馆")]


def test_uploads_need_code_but_images_are_public(env):
    c, a, _, _ = env
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (50, 50), (200, 0, 0)).save(buf, "JPEG")
    files = {"file": ("a.jpg", buf.getvalue(), "image/jpeg")}
    assert c.post("/api/uploads", files=files).status_code == 401
    r = c.post("/api/uploads", files=files, headers=h(a)).json()
    assert c.get(r["url"]).status_code == 200  # <img> 带不了请求头，靠随机 id
