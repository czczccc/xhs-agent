"""店主反馈：上报接口、以最后一次评分为准、按店/按内容类型汇总。"""

from fastapi.testclient import TestClient

from xhs_agent.feedback import JsonlEventStore, summarize

SHOP_A = {"name": "巷口小馆", "shop_type": "家常菜"}
SHOP_B = {"name": "山野咖啡", "shop_type": "咖啡"}


def test_feedback_flow(agent, settings):
    from xhs_agent.web import app as web

    web._agent = agent
    c = TestClient(web.app)
    r1 = c.post("/api/runs", json={"topic": "周三酸菜鱼半价", "content_type": "活动", "shop": SHOP_A}).json()["run_id"]
    r2 = c.post("/api/runs", json={"topic": "桂花拿铁上新", "content_type": "上新", "shop": SHOP_B}).json()["run_id"]
    ev = lambda rid, **b: c.post(f"/api/runs/{rid}/events", json=b)  # noqa: E731

    assert ev(r1, kind="rating", rating="bad").status_code == 200
    assert ev(r1, kind="copy_body").status_code == 200
    assert ev(r1, kind="rating", rating="edit", comment="标题太普通").status_code == 200  # 改主意，以最后一次为准
    assert ev(r2, kind="rating", rating="ready").status_code == 200
    assert ev(r2, kind="cover_style", value="badge").status_code == 200

    assert ev(r1, kind="rating").status_code == 400  # 缺 rating
    assert ev(r1, kind="hack").status_code == 422
    assert ev("nope", kind="copy_body").status_code == 404

    events = JsonlEventStore(settings.events_file).all()
    assert len(events) == 5 and events[0].shop_name == "巷口小馆" and events[0].content_type == "活动"

    s = summarize(events)
    assert s["overall"]["runs"] == 2 and s["overall"]["rated"] == 2
    assert s["overall"]["能直接发"] == 1 and s["overall"]["改改能发"] == 1 and s["overall"]["不能用"] == 0
    assert s["overall"]["copy_rate"] == 0.5
    assert s["by_shop"]["巷口小馆"]["copy_rate"] == 1.0
    assert s["by_content_type"]["上新"]["ready_rate"] == 1.0
    assert s["comments"] == [{"run_id": r1, "shop": "巷口小馆", "rating": "改改能发", "comment": "标题太普通"}]


def test_summarize_empty():
    assert summarize([])["overall"]["ready_rate"] is None
