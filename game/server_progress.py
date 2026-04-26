"""
server_progress — per-server question tracking (asyncpg edition).

Tracks which question IDs have been asked in each guild+pool so questions
don't repeat until the full pool is exhausted (auto-reset on completion).

Schema (managed by db.py):
  server_progress(guild_id, pool_key, question_id)  — composite PK
"""

import logging

import db

log = logging.getLogger("sigmocatclash.server_progress")


async def get_asked_ids(guild_id: int, pool_key: str) -> set[str]:
    """Return the set of question IDs already asked in this guild+pool."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT question_id FROM server_progress WHERE guild_id=$1 AND pool_key=$2",
            guild_id, pool_key,
        )
    return {r["question_id"] for r in rows}


async def mark_questions_asked(guild_id: int, question_ids: list[str], pool_key: str) -> None:
    """Record that a batch of question IDs were asked in this guild+pool."""
    if not question_ids:
        return
    async with db.pool().acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO server_progress (guild_id, pool_key, question_id)
            VALUES ($1, $2, $3)
            ON CONFLICT DO NOTHING
            """,
            [(guild_id, pool_key, qid) for qid in question_ids if qid],
        )


async def reset_pool(guild_id: int, pool_key: str) -> None:
    """Clear the asked-list for this guild+pool so all questions become available again."""
    async with db.pool().acquire() as conn:
        await conn.execute(
            "DELETE FROM server_progress WHERE guild_id=$1 AND pool_key=$2",
            guild_id, pool_key,
        )
    log.info("Guild %s: pool '%s' reset.", guild_id, pool_key)


async def check_and_auto_reset(guild_id: int, pool_key: str, total_in_pool: int) -> bool:
    """
    If all questions in the pool have been asked, reset automatically.
    Returns True if a reset occurred.
    """
    if total_in_pool <= 0:
        return False
    asked = await get_asked_ids(guild_id, pool_key)
    if len(asked) >= total_in_pool:
        log.info(
            "Guild %s: all %d questions in pool '%s' played — resetting.",
            guild_id, total_in_pool, pool_key,
        )
        await reset_pool(guild_id, pool_key)
        return True
    return False


async def pool_progress(guild_id: int, pool_key: str, total_in_pool: int) -> tuple[int, int]:
    """Return (asked_count, total_count)."""
    asked = await get_asked_ids(guild_id, pool_key)
    return len(asked), total_in_pool
