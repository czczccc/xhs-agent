"""Postgres 存储：连接池、建表、导入参考笔记、运行记录。需要 `pip install -e .[pg]`。

命令行（读取 .env 里的 DATABASE_URL 和 EMBED_*）：
    xhs-db check                  检查连接、pgvector 扩展、表和笔记数量
    xhs-db init                   建表（探测 embedding 维度）+ 建 LangGraph checkpoint 表
    xhs-db import data/notes.json   校验、算向量、写入 notes 表（JSON 数组或 jsonl；按标题去重，重复导入会更新）
    xhs-db import FILE --replace    先清空 notes 表再导入
    xhs-db search "选题"           用 pgvector 检索，看看效果
    xhs-db feedback               汇总店主反馈：能直接发比例、复制率、按店/按内容类型、店主原话
    xhs-db shop add --label 巷口小馆·王姐   生成试用码（另有 shop list / limit CODE N / disable CODE / enable CODE）
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from .config import PROJECT_ROOT, Settings
from .embedding import Embedder, build_embedder, note_text, to_pgvector
from .schemas import ReferenceNote

SCHEMA_FILE = PROJECT_ROOT / "db" / "schema.sql"
# PostgresSaver 要求的连接参数
CONN_KWARGS = {"autocommit": True, "prepare_threshold": 0}


def _require_url(settings: Settings) -> str:
    if not settings.database_url:
        raise RuntimeError("请在 .env 里填写 DATABASE_URL，例如 postgresql://用户:密码@主机:5432/xhs")
    return settings.database_url


def connect(settings: Settings):
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(_require_url(settings), row_factory=dict_row, connect_timeout=10, **CONN_KWARGS)


def make_pool(settings: Settings):
    """检索、checkpointer、运行记录共用一个连接池；远程库断线后会自动换新连接。"""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    return ConnectionPool(
        _require_url(settings),
        min_size=1,
        max_size=5,
        kwargs={**CONN_KWARGS, "row_factory": dict_row, "connect_timeout": 10},
        check=ConnectionPool.check_connection,
        open=True,
    )


# ---------- 建表 ----------


def init_schema(settings: Settings, embedder: Embedder) -> int:
    from langgraph.checkpoint.postgres import PostgresSaver

    dim = len(embedder.embed(["维度探测"])[0])
    if dim > 2000:
        raise RuntimeError(
            f"embedding 输出 {dim} 维，pgvector 的 HNSW 索引最多 2000 维。"
            "请在 .env 设置 EMBED_DIMENSIONS=1024（Qwen3 系列支持），或换一个低维模型。"
        )
    with connect(settings) as conn:
        row = conn.execute(
            """SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute
               WHERE attrelid = to_regclass('notes') AND attname = 'embedding'"""
        ).fetchone()
        if row and row["t"] != f"vector({dim})":
            raise RuntimeError(
                f"notes.embedding 已是 {row['t']}，当前 embedding 模型输出 {dim} 维。"
                "换模型需要先 DROP TABLE notes 再重新 init 和 import。"
            )
        # 多条语句不能走预编译（连接默认 prepare_threshold=0）
        conn.execute(SCHEMA_FILE.read_text(encoding="utf-8").replace("vector(1024)", f"vector({dim})"), prepare=False)
        PostgresSaver(conn).setup()
    return dim


# ---------- 导入笔记 ----------


def parse_likes(v) -> int:
    """兼容 1234 / "1234" / "1.2万" / "1.2w" / "3k" / "" 这些写法。"""
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().lower().replace(",", "").replace("+", "")
    m = re.fullmatch(r"([\d.]+)\s*(万|w|k|千)?", s)
    if not m:
        raise ValueError(f"无法识别的点赞数：{v!r}")
    mult = {"万": 10000, "w": 10000, "k": 1000, "千": 1000}.get(m.group(2) or "", 1)
    return int(float(m.group(1)) * mult)


SHOP_TYPE_ALIASES = {"咖啡店": "咖啡", "咖啡馆": "咖啡", "奶茶店": "奶茶", "甜品店": "甜品", "烧烤店": "烧烤"}
TITLE_MAX_SANE = 50  # 小红书标题上限 20 字；超过 50 基本是把正文抓进了标题字段


def _read_records(path: Path) -> list[tuple[str, object]]:
    """支持 JSON 数组和 jsonl 两种格式。返回 [(位置描述, 原始对象或解析异常)]。"""
    text = path.read_text(encoding="utf-8-sig")
    if text.lstrip().startswith("["):
        return [(f"第 {i} 条", r) for i, r in enumerate(json.loads(text), 1)]
    out: list[tuple[str, object]] = []
    for no, line in enumerate(text.splitlines(), 1):
        if line.strip():
            try:
                out.append((f"第 {no} 行", json.loads(line)))
            except json.JSONDecodeError as e:
                out.append((f"第 {no} 行", e))
    return out


def _clean(raw: dict) -> tuple[ReferenceNote, list[str]]:
    fixes: list[str] = []
    title, body = str(raw.get("title") or "").strip(), str(raw.get("body") or "").strip()
    if len(title) > TITLE_MAX_SANE:
        if len(title) > len(body):
            body = title
        title = re.split(r"[。！？!?\n]", title, maxsplit=1)[0][:20]
        fixes.append(f"标题过长，已移入正文，标题改为「{title}」")
    if not title:
        raise ValueError("缺少 title")
    body = re.sub(r"(……|…|\.{3})+$", "", body).rstrip()  # 列表页摘要末尾的省略号
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = re.split(r"[\s,，#]+", tags)
    inline = re.findall(r"#([^\s#\[]+)(?:\[话题\])?#?", body)
    body = re.sub(r"#[^\s#\[]+(?:\[话题\])?#?", "", body).strip()
    tags = list(dict.fromkeys(t.strip().lstrip("#") for t in [*tags, *inline] if t.strip().lstrip("#")))
    counts = {k: parse_likes(raw[k]) for k in ("collects", "comments") if raw.get(k) not in (None, "")}
    labels = {k: str(raw[k]).strip() for k in ("shop_type", "content_type", "author_type", "city") if raw.get(k)}
    if "shop_type" in labels:
        labels["shop_type"] = SHOP_TYPE_ALIASES.get(labels["shop_type"], labels["shop_type"])
    note = ReferenceNote(
        title=title, body=body, tags=tags, likes=parse_likes(raw.get("likes")), category=raw.get("category"),
        **counts, **labels,
    )
    return note, fixes


def load_notes(path: Path) -> tuple[list[ReferenceNote], list[str]]:
    """读取笔记文件，返回 (合法笔记, 提示列表)。

    正文可以为空（标题和点赞数仍有参考价值）；同标题只保留第一条；正文里的 #话题 并入 tags。
    提示里「跳过」表示该条没导入，「修正」表示已自动修好。
    """
    notes: dict[str, ReferenceNote] = {}
    problems: list[str] = []
    for where, raw in _read_records(path):
        try:
            if isinstance(raw, Exception):
                raise raw
            if not isinstance(raw, dict):
                raise ValueError("不是 JSON 对象")
            note, fixes = _clean(raw)
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            problems.append(f"跳过 {where}：{e}")
            continue
        if note.title in notes:
            problems.append(f"跳过 {where}：标题重复「{note.title}」")
            continue
        problems.extend(f"修正 {where}：{f}" for f in fixes)
        notes[note.title] = note
    return list(notes.values()), problems


NOTE_COLUMNS = (
    "title", "body", "tags", "category", "likes",
    "collects", "comments", "shop_type", "content_type", "author_type", "city",
)
UPSERT = (
    f"INSERT INTO notes ({', '.join(NOTE_COLUMNS)}, embedding) "
    f"VALUES ({', '.join(f'%({c})s' for c in NOTE_COLUMNS)}, %(embedding)s::vector) "
    "ON CONFLICT (title) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in (*NOTE_COLUMNS[1:], "embedding"))
)


def import_notes(
    settings: Settings, embedder: Embedder, notes: list[ReferenceNote], batch: int = 20, replace: bool = False
) -> int:
    with connect(settings) as conn:
        if replace:
            n = conn.execute("DELETE FROM notes").rowcount
            print(f"  已清空原有笔记 {n} 篇")
        for i in range(0, len(notes), batch):
            chunk = notes[i : i + batch]
            vecs = embedder.embed([note_text(n.title, n.body, n.tags) for n in chunk])
            with conn.cursor() as cur:
                cur.executemany(
                    UPSERT,
                    [{**n.model_dump(include=set(NOTE_COLUMNS)), "embedding": to_pgvector(v)} for n, v in zip(chunk, vecs)],
                )
            print(f"  已写入 {min(i + batch, len(notes))}/{len(notes)}")
    return len(notes)


# ---------- 运行记录 ----------


class RunStore:
    """把每次运行的最新状态写进 runs 表。"""

    SQL = """
        INSERT INTO runs (run_id, topic, status, result, shop_code) VALUES (%s, %s, %s, %s::jsonb, %s)
        ON CONFLICT (run_id) DO UPDATE SET status = EXCLUDED.status, result = EXCLUDED.result, updated_at = now()
    """

    def __init__(self, pool):
        self.pool = pool

    def save(self, state: dict) -> None:
        dump = lambda v: v.model_dump() if hasattr(v, "model_dump") else v  # noqa: E731
        result = {k: dump(state.get(k)) for k in ("plan", "draft", "review", "cover_path", "attempts", "error")}
        with self.pool.connection() as conn:
            conn.execute(
                self.SQL,
                (state["run_id"], state["request"].topic, state.get("status") or "running",
                 json.dumps(result, ensure_ascii=False, default=str), state["request"].shop_code),
            )


# ---------- 命令行 ----------


def check(settings: Settings) -> None:
    with connect(settings) as conn:
        print("连接成功：", conn.execute("SELECT version() AS v").fetchone()["v"].split(",")[0])
        ext = conn.execute(
            "SELECT default_version, installed_version FROM pg_available_extensions WHERE name = 'vector'"
        ).fetchone()
        if not ext:
            print("✗ 服务器没有安装 pgvector 扩展（需要在 VPS 上装 postgresql-<版本>-pgvector 包）")
        else:
            print(f"pgvector：可用 {ext['default_version']}，已启用 {ext['installed_version'] or '否（xhs-db init 会启用）'}")
        for t in ("notes", "runs", "checkpoints"):
            exists = conn.execute("SELECT to_regclass(%s) IS NOT NULL AS e", (t,)).fetchone()["e"]
            extra = ""
            if exists and t in ("notes", "runs"):
                extra = f"，{conn.execute(f'SELECT count(*) AS n FROM {t}').fetchone()['n']} 行"
            print(f"表 {t}：{'存在' + extra if exists else '不存在'}")


def shop_command(settings: Settings, args) -> None:
    from .feedback import JsonlEventStore, PgEventStore
    from .shops import JsonShopStore, PgShopStore

    pool = make_pool(settings) if settings.storage == "postgres" else None
    shops = PgShopStore(pool) if pool else JsonShopStore(settings.shops_file)
    try:
        if args.shop_cmd == "add":
            s = shops.create(args.label, args.limit)
            print(f"试用码：{s.code}（{s.label}，每天 {s.daily_limit} 次）")
            print("把这个码发给店主，打开页面后输入即可。")
        elif args.shop_cmd in ("limit", "disable", "enable"):
            if not shops.get(args.code):
                raise SystemExit(f"没有这个试用码：{args.code}")
            if args.shop_cmd == "limit":
                shops.update(args.code, daily_limit=args.n)
            else:
                shops.update(args.code, active=args.shop_cmd == "enable")
            s = shops.get(args.code)
            print(f"{s.code}：每天 {s.daily_limit} 次，{'启用' if s.active else '已停用'}")
        else:
            events = (PgEventStore(pool) if pool else JsonlEventStore(settings.events_file)).all()
            last_rating: dict[str, str] = {}
            runs: dict[str, set] = {}
            for e in events:
                runs.setdefault(e.shop_code, set()).add(e.run_id)
                if e.kind == "rating":
                    last_rating[e.run_id] = e.rating
            for s in shops.all():
                rated = [last_rating[r] for r in runs.get(s.code, ()) if r in last_rating]
                name = s.profile.name if s.profile else "（还没建档）"
                print(
                    f"{s.code}  {'  ' if s.active else '停'}  {s.label} / {name}  今日 {s.used_today}/{s.daily_limit}  "
                    f"反馈 {len(rated)} 篇，能直接发 {rated.count('ready')}"
                )
    finally:
        if pool:
            pool.close()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="xhs-db", description="小红书 Agent 数据库工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("init")
    p_imp = sub.add_parser("import")
    p_imp.add_argument("file", type=Path)
    p_imp.add_argument("--dry-run", action="store_true", help="只校验文件，不调接口、不写库")
    p_imp.add_argument("--replace", action="store_true", help="导入前清空 notes 表（替换掉示例笔记）")
    p_s = sub.add_parser("search")
    p_s.add_argument("query")
    p_s.add_argument("-k", type=int, default=5)
    p_fb = sub.add_parser("feedback", help="汇总店主反馈（STORAGE=postgres 读 events 表，否则读 logs/events.jsonl）")
    p_fb.add_argument("--json", action="store_true", help="输出 JSON")
    p_shop = sub.add_parser("shop", help="管理试用码")
    shop_sub = p_shop.add_subparsers(dest="shop_cmd", required=True)
    p_add = shop_sub.add_parser("add", help="生成一个试用码")
    p_add.add_argument("--label", required=True, help="你自己记的备注，例如：巷口小馆·王姐")
    p_add.add_argument("--limit", type=int, default=20, help="每天最多生成/重写几次")
    shop_sub.add_parser("list", help="列出所有试用码、今日用量和反馈")
    p_lim = shop_sub.add_parser("limit", help="改每日额度")
    p_lim.add_argument("code")
    p_lim.add_argument("n", type=int)
    for name, help_ in (("disable", "停用"), ("enable", "恢复")):
        shop_sub.add_parser(name, help=help_).add_argument("code")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = Settings.from_env()
    if args.cmd == "check":
        check(settings)
    elif args.cmd == "init":
        dim = init_schema(settings, build_embedder(settings))
        print(f"建表完成，向量维度 {dim}")
    elif args.cmd == "import":
        notes, problems = load_notes(args.file)
        for p in problems:
            print("  ⚠", p)
        skipped = sum(p.startswith("跳过") for p in problems)
        print(f"可导入 {len(notes)} 篇，跳过 {skipped} 条，自动修正 {len(problems) - skipped} 处")
        if not args.dry_run and notes:
            import_notes(settings, build_embedder(settings), notes, replace=args.replace)
            print("导入完成")
    elif args.cmd == "search":
        from .retrieval import PgVectorRetriever

        pool = make_pool(settings)
        for n in PgVectorRetriever(pool, build_embedder(settings)).search(args.query, args.k):
            print(f"{n.score:.3f}  [{n.category or '-'}] {n.title}（{n.likes} 赞）")
        pool.close()
    elif args.cmd == "shop":
        shop_command(settings, args)
    elif args.cmd == "feedback":
        from .feedback import JsonlEventStore, PgEventStore, print_summary, summarize

        pool = make_pool(settings) if settings.storage == "postgres" else None
        store = PgEventStore(pool) if pool else JsonlEventStore(settings.events_file)
        s = summarize(store.all())
        print(json.dumps(s, ensure_ascii=False, indent=2)) if args.json else print_summary(s)
        if pool:
            pool.close()


if __name__ == "__main__":
    main()
