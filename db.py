"""
asyncpg connection pool — initialized once by bot.py on startup.

All persistent modules acquire connections from this shared pool.
Call db.init(dsn) before any DB operations; call db.close() on shutdown.
"""

import logging
from typing import Optional

import asyncpg

log = logging.getLogger("sigmocatclash.db")

_pool: Optional[asyncpg.Pool] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    guild_id       BIGINT   NOT NULL,
    player_id      BIGINT   NOT NULL,
    name           TEXT     NOT NULL,
    total_score    INTEGER  NOT NULL DEFAULT 0,
    games_played   INTEGER  NOT NULL DEFAULT 0,
    best_score     INTEGER  NOT NULL DEFAULT 0,
    wins           INTEGER  NOT NULL DEFAULT 0,
    total_answers  INTEGER  NOT NULL DEFAULT 0,
    current_streak INTEGER  NOT NULL DEFAULT 0,
    longest_streak INTEGER  NOT NULL DEFAULT 0,
    last_played    DATE,
    achievements   TEXT[]   NOT NULL DEFAULT '{}',
    PRIMARY KEY (guild_id, player_id)
);

CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id             BIGINT  PRIMARY KEY,
    reminder_channel_id  BIGINT,
    timezone             TEXT    NOT NULL DEFAULT 'UTC',
    last_reminded_date   DATE
);

CREATE TABLE IF NOT EXISTS server_progress (
    guild_id     BIGINT  NOT NULL,
    pool_key     TEXT    NOT NULL,
    question_id  TEXT    NOT NULL,
    PRIMARY KEY (guild_id, pool_key, question_id)
);
"""


async def init(dsn: str) -> None:
    """Create the connection pool and ensure all tables exist."""
    global _pool
    _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10, command_timeout=30)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
    log.info("Database ready — pool established.")


async def close() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        log.info("Database pool closed.")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database not initialized — call db.init() before any queries.")
    return _pool
