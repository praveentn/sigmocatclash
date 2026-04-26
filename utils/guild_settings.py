"""
Per-guild configuration — asyncpg edition.

Schema (managed by db.py):
  guild_settings(guild_id PK, reminder_channel_id, timezone, last_reminded_date)
"""

import logging

import db

log = logging.getLogger("sigmocatclash.guild_settings")


def _row_to_dict(row) -> dict:
    d = dict(row)
    lrd = d.get("last_reminded_date")
    d["last_reminded_date"] = lrd.isoformat() if lrd is not None else ""
    return d


async def get_guild_settings(guild_id: int) -> dict | None:
    """Return settings for a guild, or None if reminders aren't configured."""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM guild_settings WHERE guild_id=$1", guild_id,
        )
    return _row_to_dict(row) if row else None


async def save_guild_settings(guild_id: int, settings: dict) -> None:
    """Persist (or update) settings for a guild."""
    last = settings.get("last_reminded_date") or None
    if last == "":
        last = None
    async with db.pool().acquire() as conn:
        await conn.execute(
            """
            INSERT INTO guild_settings (guild_id, reminder_channel_id, timezone, last_reminded_date)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (guild_id) DO UPDATE SET
                reminder_channel_id = EXCLUDED.reminder_channel_id,
                timezone            = EXCLUDED.timezone,
                last_reminded_date  = EXCLUDED.last_reminded_date
            """,
            guild_id,
            settings.get("reminder_channel_id"),
            settings.get("timezone", "UTC"),
            last,
        )


async def remove_guild_settings(guild_id: int) -> None:
    """Remove a guild's reminder settings (disables all reminders)."""
    async with db.pool().acquire() as conn:
        await conn.execute("DELETE FROM guild_settings WHERE guild_id=$1", guild_id)


async def get_all_guild_settings() -> dict[str, dict]:
    """Return all guild settings keyed by guild_id string."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch("SELECT * FROM guild_settings")
    return {str(r["guild_id"]): _row_to_dict(r) for r in rows}
