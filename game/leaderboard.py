"""
Per-guild persistent leaderboard — asyncpg edition.

All public functions are async and require guild_id as a keyword-only argument
so data never leaks between Discord servers.

Schema (managed by db.py):
  players(guild_id, player_id, name, total_score, games_played, best_score,
          wins, total_answers, current_streak, longest_streak, last_played, achievements)
"""

import logging
from datetime import date
from typing import Optional

import db
from game.achievements import check_new_achievements

log = logging.getLogger("sigmocatclash.leaderboard")

# Discord display/nick names are ≤ 32 chars; cap slightly higher for safety
_MAX_NAME_LEN = 40

# Rank tiers (min_score, display_name, emoji) — must remain sorted ascending
RANKS = [
    (0,    "Rookie",  "🪨"),
    (50,   "Player",  "🥉"),
    (150,  "Pro",     "🥈"),
    (350,  "Elite",   "🥇"),
    (700,  "Master",  "💎"),
    (1200, "Legend",  "👑"),
]


# ── Rank helpers ──────────────────────────────────────────────────────────────

def get_rank(total_score: int) -> tuple[str, str, int]:
    """
    Return (rank_name, rank_emoji, next_threshold).
    next_threshold is -1 when the player is at the maximum rank.
    """
    rank_name, rank_emoji, rank_idx = RANKS[0][1], RANKS[0][2], 0
    for i, (threshold, name, emoji) in enumerate(RANKS):
        if total_score >= threshold:
            rank_name, rank_emoji, rank_idx = name, emoji, i
        else:
            break
    next_threshold = RANKS[rank_idx + 1][0] if rank_idx + 1 < len(RANKS) else -1
    return rank_name, rank_emoji, next_threshold


# ── Row conversion ────────────────────────────────────────────────────────────

def _row_to_dict(row) -> dict:
    """Convert an asyncpg Record to a plain dict with consistent field types."""
    d = dict(row)
    # last_played comes back as datetime.date; normalise to isoformat string
    lp = d.get("last_played")
    d["last_played"] = lp.isoformat() if lp is not None else ""
    # achievements comes back as list[str] from TEXT[] — already correct
    if d.get("achievements") is None:
        d["achievements"] = []
    return d


# ── Daily streak ──────────────────────────────────────────────────────────────

def _update_daily_streak(entry: dict, today_str: str) -> None:
    """Increment, maintain, or reset daily streak based on last_played date."""
    last_played = entry.get("last_played", "")

    if last_played == today_str:
        return

    if last_played:
        try:
            delta = (date.fromisoformat(today_str) - date.fromisoformat(str(last_played))).days
            entry["current_streak"] = entry.get("current_streak", 0) + 1 if delta == 1 else 1
        except ValueError:
            entry["current_streak"] = 1
    else:
        entry["current_streak"] = 1

    entry["last_played"] = today_str
    if entry["current_streak"] > entry.get("longest_streak", 0):
        entry["longest_streak"] = entry["current_streak"]


# ── Public API ────────────────────────────────────────────────────────────────

async def record_game_results(
    player_scores: dict[int, tuple[str, int]],
    player_answers: dict[int, int] | None = None,
    *,
    guild_id: int,
) -> dict[int, list[str]]:
    """
    Persist results from a completed game into the per-guild leaderboard.

    player_scores  : {player_id: (display_name, score)}
    player_answers : {player_id: valid_answers_count}  (optional)
    guild_id       : Discord guild snowflake — required; enforces server isolation

    Returns: {player_id: [newly_earned_achievement_id, ...]}
    """
    if not player_scores:
        return {}

    player_answers = player_answers or {}
    today_str = date.today().isoformat()
    max_score = max((s for _, s in player_scores.values()), default=0)
    winners = {pid for pid, (_, s) in player_scores.items() if s == max_score and s > 0}
    new_achievements: dict[int, list[str]] = {}

    async with db.pool().acquire() as conn:
        for player_id, (name, score) in player_scores.items():
            safe_name = name[:_MAX_NAME_LEN]

            row = await conn.fetchrow(
                "SELECT * FROM players WHERE guild_id=$1 AND player_id=$2",
                guild_id, player_id,
            )
            entry = _row_to_dict(row) if row else {
                "name": safe_name, "total_score": 0, "games_played": 0,
                "best_score": 0, "wins": 0, "total_answers": 0,
                "current_streak": 0, "longest_streak": 0,
                "last_played": "", "achievements": [],
            }

            entry["name"] = safe_name
            entry["total_score"] = entry.get("total_score", 0) + score
            entry["games_played"] = entry.get("games_played", 0) + 1
            if score > entry.get("best_score", 0):
                entry["best_score"] = score
            if player_id in winners:
                entry["wins"] = entry.get("wins", 0) + 1
            entry["total_answers"] = (
                entry.get("total_answers", 0) + player_answers.get(player_id, 0)
            )
            _update_daily_streak(entry, today_str)

            earned = check_new_achievements(entry)
            if earned:
                new_achievements[player_id] = earned

            await conn.execute(
                """
                INSERT INTO players
                    (guild_id, player_id, name, total_score, games_played, best_score,
                     wins, total_answers, current_streak, longest_streak,
                     last_played, achievements)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (guild_id, player_id) DO UPDATE SET
                    name           = EXCLUDED.name,
                    total_score    = EXCLUDED.total_score,
                    games_played   = EXCLUDED.games_played,
                    best_score     = EXCLUDED.best_score,
                    wins           = EXCLUDED.wins,
                    total_answers  = EXCLUDED.total_answers,
                    current_streak = EXCLUDED.current_streak,
                    longest_streak = EXCLUDED.longest_streak,
                    last_played    = EXCLUDED.last_played,
                    achievements   = EXCLUDED.achievements
                """,
                guild_id, player_id, safe_name,
                entry["total_score"], entry["games_played"],
                entry["best_score"], entry["wins"], entry["total_answers"],
                entry["current_streak"], entry["longest_streak"],
                entry["last_played"] or None,
                entry.get("achievements", []),
            )

    return new_achievements


async def get_overall_leaderboard(top_n: int = 15, *, guild_id: int) -> list[dict]:
    """Return top_n players in the guild sorted by total_score descending."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id=$1 ORDER BY total_score DESC LIMIT $2",
            guild_id, top_n,
        )
    return [_row_to_dict(r) for r in rows]


async def get_streak_leaderboard(top_n: int = 10, *, guild_id: int) -> list[dict]:
    """Return top_n players in the guild sorted by current_streak (streak > 0 only)."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id=$1 AND current_streak > 0 "
            "ORDER BY current_streak DESC LIMIT $2",
            guild_id, top_n,
        )
    return [_row_to_dict(r) for r in rows]


async def get_player_stats(player_id: int, *, guild_id: int) -> dict | None:
    """Return full stats for a player in this guild, or None if not found."""
    async with db.pool().acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM players WHERE guild_id=$1 AND player_id=$2",
            guild_id, player_id,
        )
    return _row_to_dict(row) if row else None


async def get_guild_players(*, guild_id: int) -> list[dict]:
    """Return all players in this guild; each dict includes 'player_id' as int."""
    async with db.pool().acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM players WHERE guild_id=$1",
            guild_id,
        )
    return [_row_to_dict(r) for r in rows]
