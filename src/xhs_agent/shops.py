"""试用码：每家试用店一个码，同时管访问权限、店铺档案、每日额度和反馈归属。

- 码是 6 位大写字母数字，去掉容易看错的 0/O、1/I/L；输入不分大小写
- 档案存服务端，换手机输同一个码就回来；生成时以服务端档案为准
- 每日额度按「生成 + 重写」计次（都会花模型额度），追问不计
- STORAGE=postgres 存 shops / shop_usage 表；否则存一个 JSON 文件（本地开发用）
"""

from __future__ import annotations

import json
import secrets
from datetime import date
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from .schemas import ShopProfile

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_LEN = 6
DEFAULT_DAILY_LIMIT = 20


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def normalize(code: str | None) -> str:
    return (code or "").strip().upper()


class Shop(BaseModel):
    code: str
    label: str = ""  # 你自己记的备注，例如「巷口小馆·王姐」
    profile: ShopProfile | None = None
    daily_limit: int = DEFAULT_DAILY_LIMIT
    active: bool = True
    used_today: int = 0


class ShopStore(Protocol):
    def create(self, label: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> Shop: ...
    def get(self, code: str) -> Shop | None: ...
    def save_profile(self, code: str, profile: ShopProfile) -> None: ...
    def update(self, code: str, *, daily_limit: int | None = None, active: bool | None = None) -> None: ...
    def consume(self, code: str) -> bool: ...
    def all(self) -> list[Shop]: ...


class JsonShopStore:
    """本地开发用：整个文件读写，每次都读最新的（CLI 改了额度立即生效）。"""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {"shops": {}, "usage": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _dump(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _shop(self, data: dict, code: str) -> Shop:
        raw = data["shops"][code]
        used = data["usage"].get(code, {}).get(date.today().isoformat(), 0)
        return Shop(code=code, **raw, used_today=used)

    def create(self, label: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> Shop:
        data = self._load()
        code = new_code()
        while code in data["shops"]:
            code = new_code()
        data["shops"][code] = {"label": label, "profile": None, "daily_limit": daily_limit, "active": True}
        self._dump(data)
        return self._shop(data, code)

    def get(self, code: str) -> Shop | None:
        data, code = self._load(), normalize(code)
        return self._shop(data, code) if code in data["shops"] else None

    def save_profile(self, code: str, profile: ShopProfile) -> None:
        data = self._load()
        data["shops"][normalize(code)]["profile"] = profile.model_dump()
        self._dump(data)

    def update(self, code: str, *, daily_limit: int | None = None, active: bool | None = None) -> None:
        data = self._load()
        raw = data["shops"][normalize(code)]
        if daily_limit is not None:
            raw["daily_limit"] = daily_limit
        if active is not None:
            raw["active"] = active
        self._dump(data)

    def consume(self, code: str) -> bool:
        data, code = self._load(), normalize(code)
        shop = data["shops"][code]
        day = data["usage"].setdefault(code, {})
        today = date.today().isoformat()
        if day.get(today, 0) >= shop["daily_limit"]:
            return False
        day[today] = day.get(today, 0) + 1
        self._dump(data)
        return True

    def all(self) -> list[Shop]:
        data = self._load()
        return [self._shop(data, c) for c in data["shops"]]


class PgShopStore:
    def __init__(self, pool):
        self.pool = pool

    SELECT = """
        SELECT s.code, s.label, s.profile, s.daily_limit, s.active, COALESCE(u.count, 0) AS used_today
        FROM shops s LEFT JOIN shop_usage u ON u.code = s.code AND u.day = CURRENT_DATE
    """

    def create(self, label: str, daily_limit: int = DEFAULT_DAILY_LIMIT) -> Shop:
        with self.pool.connection() as conn:
            while True:
                code = new_code()
                row = conn.execute(
                    "INSERT INTO shops (code, label, daily_limit) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING code",
                    (code, label, daily_limit),
                ).fetchone()
                if row:
                    return self.get(code)

    def get(self, code: str) -> Shop | None:
        with self.pool.connection() as conn:
            r = conn.execute(self.SELECT + " WHERE s.code = %s", (normalize(code),)).fetchone()
        return Shop(**r) if r else None

    def save_profile(self, code: str, profile: ShopProfile) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE shops SET profile = %s::jsonb, updated_at = now() WHERE code = %s",
                (profile.model_dump_json(), normalize(code)),
            )

    def update(self, code: str, *, daily_limit: int | None = None, active: bool | None = None) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "UPDATE shops SET daily_limit = COALESCE(%s, daily_limit), active = COALESCE(%s, active), "
                "updated_at = now() WHERE code = %s",
                (daily_limit, active, normalize(code)),
            )

    def consume(self, code: str) -> bool:
        """原子地 +1；超额时不加并返回 False。"""
        with self.pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO shop_usage (code, day, count)
                SELECT code, CURRENT_DATE, 1 FROM shops WHERE code = %(c)s AND daily_limit > 0
                ON CONFLICT (code, day) DO UPDATE SET count = shop_usage.count + 1
                WHERE shop_usage.count < (SELECT daily_limit FROM shops WHERE code = %(c)s)
                RETURNING count
                """,
                {"c": normalize(code)},
            ).fetchone()
        return row is not None

    def all(self) -> list[Shop]:
        with self.pool.connection() as conn:
            rows = conn.execute(self.SELECT + " ORDER BY s.created_at").fetchall()
        return [Shop(**r) for r in rows]
