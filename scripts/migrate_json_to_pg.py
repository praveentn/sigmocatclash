"""
One-time migration: copy data/overall_leaderboard.json, data/server_progress.json,
and data/guild_settings.json into PostgreSQL.

Run once on Railway (or locally with DATABASE_URL set):
  python scripts/migrate_json_to_pg.py

Idempotent — ON CONFLICT DO NOTHING means re-running won't duplicate rows.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"

SCHEMA = """
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


async def migrate(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        print("Creating tables (if not exist)…")
        await conn.execute(SCHEMA)

        # ── Players (overall_leaderboard.json) ────────────────────────────────
        lb_file = DATA / "overall_leaderboard.json"
        if lb_file.exists():
            lb_data = json.loads(lb_file.read_text(encoding="utf-8"))
            player_rows = 0
            for guild_id_str, guild_obj in lb_data.get("guilds", {}).items():
                guild_id = int(guild_id_str)
                for player_id_str, p in guild_obj.get("players", {}).items():
                    player_id = int(player_id_str)
                    last_played = p.get("last_played") or None
                    if last_played == "":
                        last_played = None
                    achievements = p.get("achievements", [])
                    await conn.execute(
                        """
                        INSERT INTO players
                            (guild_id, player_id, name, total_score, games_played,
                             best_score, wins, total_answers, current_streak,
                             longest_streak, last_played, achievements)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                        ON CONFLICT DO NOTHING
                        """,
                        guild_id, player_id,
                        p.get("name", "Unknown")[:40],
                        p.get("total_score", 0),
                        p.get("games_played", 0),
                        p.get("best_score", 0),
                        p.get("wins", 0),
                        p.get("total_answers", 0),
                        p.get("current_streak", 0),
                        p.get("longest_streak", 0),
                        last_played,
                        achievements,
                    )
                    player_rows += 1
            print(f"  players: {player_rows} row(s) migrated from {lb_file.name}")
        else:
            print(f"  players: {lb_file.name} not found — skipping")

        # ── Guild settings (guild_settings.json) ──────────────────────────────
        gs_file = DATA / "guild_settings.json"
        if gs_file.exists():
            gs_data = json.loads(gs_file.read_text(encoding="utf-8"))
            gs_rows = 0
            for guild_id_str, s in gs_data.items():
                guild_id = int(guild_id_str)
                last = s.get("last_reminded_date") or None
                if last == "":
                    last = None
                await conn.execute(
                    """
                    INSERT INTO guild_settings
                        (guild_id, reminder_channel_id, timezone, last_reminded_date)
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT DO NOTHING
                    """,
                    guild_id,
                    s.get("reminder_channel_id"),
                    s.get("timezone", "UTC"),
                    last,
                )
                gs_rows += 1
            print(f"  guild_settings: {gs_rows} row(s) migrated from {gs_file.name}")
        else:
            print(f"  guild_settings: {gs_file.name} not found — skipping")

        # ── Server progress (server_progress.json) ────────────────────────────
        sp_file = DATA / "server_progress.json"
        if sp_file.exists():
            sp_data = json.loads(sp_file.read_text(encoding="utf-8"))
            sp_rows = 0
            rows_to_insert = []
            for guild_id_str, pools in sp_data.items():
                guild_id = int(guild_id_str)
                for pool_key, question_ids in pools.items():
                    for qid in question_ids:
                        if qid:
                            rows_to_insert.append((guild_id, pool_key, qid))
            if rows_to_insert:
                await conn.executemany(
                    """
                    INSERT INTO server_progress (guild_id, pool_key, question_id)
                    VALUES ($1, $2, $3)
                    ON CONFLICT DO NOTHING
                    """,
                    rows_to_insert,
                )
                sp_rows = len(rows_to_insert)
            print(f"  server_progress: {sp_rows} row(s) migrated from {sp_file.name}")
        else:
            print(f"  server_progress: {sp_file.name} not found — skipping")

        print("\nMigration complete ✓")
        print("You can now delete the JSON files from data/ if desired.")

    finally:
        await conn.close()


if __name__ == "__main__":
    dsn = os.getenv("DATABASE_URL", "").strip()
    if not dsn:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    asyncio.run(migrate(dsn))
