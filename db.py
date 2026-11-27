import asyncpg
import json
import time
from typing import Optional, List, Dict, Any
from datetime import datetime
import config

_pool: Optional[asyncpg.Pool] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id        BIGINT PRIMARY KEY,
    username       TEXT,
    first_seen     TIMESTAMPTZ DEFAULT NOW(),
    last_seen      TIMESTAMPTZ DEFAULT NOW(),
    banned         BOOLEAN DEFAULT FALSE,
    hits_total     INTEGER DEFAULT 0,
    hits_charged   INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS proxies (
    id             SERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL,
    raw            TEXT NOT NULL,
    server         TEXT NOT NULL,
    username       TEXT,
    password       TEXT,
    country        TEXT,
    alive          BOOLEAN DEFAULT TRUE,
    fail_count     INTEGER DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, raw)
);
CREATE INDEX IF NOT EXISTS idx_proxies_user ON proxies(user_id);

CREATE TABLE IF NOT EXISTS hits (
    id             BIGSERIAL PRIMARY KEY,
    user_id        BIGINT NOT NULL,
    gateway        TEXT NOT NULL,
    url            TEXT,
    card           TEXT NOT NULL,
    bin_country    TEXT,
    brand          TEXT,
    status         TEXT NOT NULL,
    decline_code   TEXT,
    error_message  TEXT,
    response_ms    INTEGER,
    raw_response   JSONB,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_hits_user ON hits(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_hits_status ON hits(status, created_at DESC);
"""


async def init():
    global _pool
    _pool = await asyncpg.create_pool(
        config.DATABASE_URL, min_size=2, max_size=20, command_timeout=30,
    )
    async with _pool.acquire() as conn:
        await conn.execute(SCHEMA)


async def close():
    if _pool:
        await _pool.close()


async def upsert_user(user_id: int, username: str = ""):
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO users (user_id, username, last_seen)
               VALUES ($1, $2, NOW())
               ON CONFLICT (user_id) DO UPDATE
               SET last_seen = NOW(), username = COALESCE(NULLIF($2,''), users.username)""",
            user_id, username or "",
        )


async def is_banned(user_id: int) -> bool:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow("SELECT banned FROM users WHERE user_id = $1", user_id)
        return bool(row and row["banned"])


async def ban_user(user_id: int, banned: bool = True):
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE users SET banned = $2 WHERE user_id = $1", user_id, banned)


async def save_hit(user_id: int, gateway: str, url: str, card: str,
                   status: str, decline_code: Optional[str], error: Optional[str],
                   response_ms: int, raw: Optional[dict], bin_country: str = "",
                   brand: str = ""):
    async with _pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO hits (user_id, gateway, url, card, bin_country, brand,
                                 status, decline_code, error_message, response_ms, raw_response)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            user_id, gateway, url, card, bin_country, brand, status,
            decline_code, error, response_ms,
            json.dumps(raw) if raw else None,
        )
        await conn.execute(
            "UPDATE users SET hits_total = hits_total + 1 WHERE user_id = $1", user_id,
        )
        if status == "CHARGED":
            await conn.execute(
                "UPDATE users SET hits_charged = hits_charged + 1 WHERE user_id = $1", user_id,
            )


async def user_stats(user_id: int) -> Dict[str, Any]:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT hits_total, hits_charged FROM users WHERE user_id = $1", user_id)
        if not row:
            return {"total": 0, "charged": 0, "live": 0}
        live_row = await conn.fetchrow(
            """SELECT COUNT(*) FROM hits
               WHERE user_id = $1 AND status IN ('CHARGED','LIVE','3DS_CHALLENGE')""",
            user_id)
        return {"total": row["hits_total"], "charged": row["hits_charged"],
                "live": live_row[0] if live_row else 0}


async def add_proxies(user_id: int, parsed: List[dict]) -> int:
    added = 0
    async with _pool.acquire() as conn:
        for p in parsed:
            try:
                await conn.execute(
                    """INSERT INTO proxies (user_id, raw, server, username, password)
                       VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (user_id, raw) DO NOTHING""",
                    user_id, p["raw"], p["server"], p.get("username"), p.get("password"),
                )
                added += 1
            except Exception:
                continue
    return added


async def get_proxies(user_id: int, alive_only: bool = True) -> List[dict]:
    async with _pool.acquire() as conn:
        q = "SELECT raw, server, username, password, country FROM proxies WHERE user_id = $1"
        if alive_only:
            q += " AND alive = TRUE"
        rows = await conn.fetch(q, user_id)
        return [dict(r) for r in rows]


async def mark_proxy_dead(user_id: int, raw: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            """UPDATE proxies SET alive = FALSE, fail_count = fail_count + 1
               WHERE user_id = $1 AND raw = $2""",
            user_id, raw,
        )


async def clear_proxies(user_id: int) -> int:
    async with _pool.acquire() as conn:
        res = await conn.execute("DELETE FROM proxies WHERE user_id = $1", user_id)
        return int(res.split()[-1])


async def proxy_count(user_id: int) -> int:
    async with _pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) FROM proxies WHERE user_id = $1 AND alive = TRUE", user_id)
        return row[0] if row else 0


async def set_proxy_country(user_id: int, raw: str, country: str):
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE proxies SET country = $3 WHERE user_id = $1 AND raw = $2",
            user_id, raw, country.upper(),
        )


async def recent_hits(user_id: int, limit: int = 10) -> List[dict]:
    async with _pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT card, status, gateway, response_ms, created_at
               FROM hits WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2""",
            user_id, limit)
        return [dict(r) for r in rows]