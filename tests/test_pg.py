"""Postgres 集成测试。需要一个可以随便清空的库：

    TEST_DATABASE_URL=postgresql://user:pass@host:5432/xhs_test pytest tests/test_pg.py

没设置 TEST_DATABASE_URL 时全部跳过。会 DROP 掉该库里的 notes / runs / checkpoint 表。
"""

import json
import os

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="未设置 TEST_DATABASE_URL")

from xhs_agent.embedding import FakeEmbedder  # noqa: E402
from xhs_agent.llm import FakeLLM  # noqa: E402
from xhs_agent.schemas import NoteRequest  # noqa: E402

NOTES = [
    {"title": "打工人带饭一周不重样🍱", "body": "周日备菜，工作日装盒带饭便当。#带饭[话题]# #便当[话题]#", "likes": "1.2万", "category": "美食",
     "collects": "3k", "shop_type": "家常菜", "content_type": "攻略", "author_type": "博主", "city": "杭州"},
    {"title": "新手健身第一个月怎么练", "body": "深蹲硬拉卧推，健身新手先学动作。", "tags": ["健身"], "likes": 356, "category": "运动"},
    {"title": "租房改造500元", "body": "出租屋改造，灯光地毯收纳，租房也能温馨。", "tags": "租房 改造", "likes": "3k", "category": "家居"},
]


@pytest.fixture
def pg_settings(settings):
    import psycopg

    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP TABLE IF EXISTS shop_usage, shops, notes, runs, events, checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations")
    settings.database_url = URL
    return settings


@pytest.fixture
def seeded(pg_settings, tmp_path):
    from xhs_agent import db

    emb = FakeEmbedder()
    assert db.init_schema(pg_settings, emb) == emb.dim
    db.init_schema(pg_settings, emb)  # 重复 init 不报错
    f = tmp_path / "notes.jsonl"
    f.write_text("\n".join(json.dumps(n, ensure_ascii=False) for n in NOTES) + "\n坏行\n", encoding="utf-8")
    notes, problems = db.load_notes(f)
    assert len(notes) == 3 and len(problems) == 1
    db.import_notes(pg_settings, emb, notes)
    db.import_notes(pg_settings, emb, notes)  # 重复导入按标题更新，不产生重复
    return pg_settings, emb


def test_import_and_search(seeded):
    from xhs_agent.db import connect, make_pool
    from xhs_agent.retrieval import PgVectorRetriever

    settings, emb = seeded
    with connect(settings) as c:
        rows = c.execute("SELECT title, tags, likes FROM notes ORDER BY likes DESC").fetchall()
    assert len(rows) == 3
    assert rows[0]["likes"] == 12000 and set(rows[0]["tags"]) == {"带饭", "便当"}

    pool = make_pool(settings)
    try:
        hits = PgVectorRetriever(pool, emb).search("打工人带饭便当", k=2)
    finally:
        pool.close()
    assert hits[0].title == "打工人带饭一周不重样🍱" and hits[0].category == "美食"
    assert (hits[0].collects, hits[0].author_type, hits[0].city) == (3000, "博主", "杭州")
    assert hits[1].collects is None and hits[1].author_type is None
    assert hits[0].score > hits[1].score


def test_dimension_mismatch_is_rejected(seeded):
    from xhs_agent import db

    settings, _ = seeded
    with pytest.raises(RuntimeError, match="维度|vector"):
        db.init_schema(settings, FakeEmbedder(dim=32))
    with pytest.raises(RuntimeError, match="EMBED_DIMENSIONS"):
        db.init_schema(settings, FakeEmbedder(dim=4096))


def test_postgres_storage_survives_restart(seeded):
    """STORAGE=postgres：新建一个 XhsAgent（相当于服务重启）后还能继续人工确认，runs 表有记录。"""
    from xhs_agent.db import connect
    from xhs_agent.graph import XhsAgent

    settings, _ = seeded
    settings.storage = "postgres"  # 检索仍用默认的 memory，这里只测状态持久化

    a1 = XhsAgent(settings=settings, llm=FakeLLM())
    state = a1.start(NoteRequest(topic="打工人一周带饭便当"))
    assert state["status"] == "pending_review"
    a1.close()

    a2 = XhsAgent(settings=settings, llm=FakeLLM())
    assert a2.get(state["run_id"])["draft"].title == state["draft"].title
    assert a2.resume(state["run_id"], "approve")["status"] == "approved"
    a2.close()

    with connect(settings) as c:
        row = c.execute("SELECT status, result FROM runs WHERE run_id = %s", (state["run_id"],)).fetchone()
    assert row["status"] == "approved" and row["result"]["draft"]["title"] == state["draft"].title


def test_pg_event_store(seeded):
    from xhs_agent.db import make_pool
    from xhs_agent.feedback import Event, PgEventStore, summarize

    settings, _ = seeded
    pool = make_pool(settings)
    try:
        store = PgEventStore(pool)
        store.add(Event(run_id="r1", kind="rating", rating="ready", shop_name="巷口小馆", content_type="活动"))
        store.add(Event(run_id="r1", kind="copy_body", shop_name="巷口小馆", content_type="活动"))
        got = store.all()
    finally:
        pool.close()
    assert [e.kind for e in got] == ["rating", "copy_body"] and got[0].rating == "ready"
    assert summarize(got)["overall"]["ready_rate"] == 1.0


def test_pg_shop_store(seeded):
    from xhs_agent.db import make_pool
    from xhs_agent.schemas import ShopProfile
    from xhs_agent.shops import PgShopStore

    settings, _ = seeded
    pool = make_pool(settings)
    try:
        store = PgShopStore(pool)
        s = store.create("A 店", daily_limit=2)
        assert store.get(s.code.lower()).label == "A 店" and store.get("ZZZZZZ") is None
        store.save_profile(s.code, ShopProfile(name="巷口小馆", shop_type="家常菜"))
        assert store.get(s.code).profile.name == "巷口小馆"
        assert [store.consume(s.code) for _ in range(3)] == [True, True, False]  # 超额不再加
        assert store.get(s.code).used_today == 2
        store.update(s.code, daily_limit=3, active=False)
        got = store.get(s.code)
        assert (got.daily_limit, got.active) == (3, False) and store.consume(s.code)
        assert [x.code for x in store.all()] == [s.code]
    finally:
        pool.close()
