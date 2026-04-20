"""
Per-guild persistent leaderboard — accumulates scores across all completed games.

Data layout  (data/overall_leaderboard.json):
  {
    "guilds": {
      "<guild_id_str>": {
        "players": {
          "<player_id_str>": {
            "name":           str,
            "total_score":    int,
            "games_played":   int,
            "best_score":     int,
            "wins":           int,
            "total_answers":  int,
            "current_streak": int,
            "longest_streak": int,
            "last_played":    "YYYY-MM-DD",
            "achievements":   [str, ...]
          }
        }
      }
    }
  }

All public functions take guild_id as a keyword-only argument so data never
leaks between Discord servers. The old global-players layout is silently ignored
on first read (migration not possible — we don't know which guild records belong to).
"""

import json
import logging
from datetime import date
from pathlib import Path

from game.achievements import check_new_achievements

log = logging.getLogger("sigmocatclash.leaderboard")

LEADERBOARD_PATH = Path(__file__).parent.parent / "data" / "overall_leaderboard.json"

# Discord display/nick names are ≤ 32 chars; cap higher for safety
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


# ── File I/O ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    if LEADERBOARD_PATH.exists():
        try:
            with open(LEADERBOARD_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to load leaderboard: %s", exc)
    return {"guilds": {}}


def _save(data: dict) -> None:
    """Write via rename for atomic replacement — prevents corrupt JSON on crash."""
    try:
        LEADERBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = LEADERBOARD_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(LEADERBOARD_PATH)  # atomic on POSIX; uses MoveFileEx on Windows
    except OSError as exc:
        log.error("Failed to save leaderboard: %s", exc)


def _guild_players(data: dict, guild_id: int) -> dict:
    """Return the mutable players dict for a guild, creating missing keys."""
    return (
        data
        .setdefault("guilds", {})
        .setdefault(str(guild_id), {"players": {}})
        .setdefault("players", {})
    )


def _read_guild_players(data: dict, guild_id: int) -> dict:
    """Return the players dict for a guild without mutating (empty dict on miss)."""
    return (
        data
        .get("guilds", {})
        .get(str(guild_id), {})
        .get("players", {})
    )


# ── Rank helpers ──────────────────────────────────────────────────────────────

def get_rank(total_score: int) -> tuple[str, str, int]:
    """
    Return (rank_name, rank_emoji, next_threshold) for a given total_score.
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


# ── Daily streak ──────────────────────────────────────────────────────────────

def _update_daily_streak(entry: dict, today_str: str) -> None:
    """Increment, maintain, or reset daily streak based on last_played date."""
    last_played = entry.get("last_played", "")

    if last_played == today_str:
        return  # already counted for today

    if last_played:
        try:
            delta = (date.fromisoformat(today_str) - date.fromisoformat(last_played)).days
            entry["current_streak"] = entry.get("current_streak", 0) + 1 if delta == 1 else 1
        except ValueError:
            entry["current_streak"] = 1
    else:
        entry["current_streak"] = 1

    entry["last_played"] = today_str
    if entry["current_streak"] > entry.get("longest_streak", 0):
        entry["longest_streak"] = entry["current_streak"]


# ── Public API ────────────────────────────────────────────────────────────────

def record_game_results(
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

    data = _load()
    players = _guild_players(data, guild_id)
    today_str = date.today().isoformat()
    player_answers = player_answers or {}

    max_score = max((s for _, s in player_scores.values()), default=0)
    # All players tied for max are co-winners (provided they scored > 0)
    winners = {pid for pid, (_, s) in player_scores.items() if s == max_score and s > 0}

    new_achievements: dict[int, list[str]] = {}

    for player_id, (name, score) in player_scores.items():
        pid_str = str(player_id)
        safe_name = name[:_MAX_NAME_LEN]
        entry = players.setdefault(pid_str, {
            "name": safe_name,
            "total_score": 0,
            "games_played": 0,
            "best_score": 0,
            "wins": 0,
            "total_answers": 0,
            "current_streak": 0,
            "longest_streak": 0,
            "last_played": "",
            "achievements": [],
        })
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

    _save(data)
    return new_achievements


def get_overall_leaderboard(top_n: int = 15, *, guild_id: int) -> list[dict]:
    """Return top_n players in the guild sorted by total_score descending."""
    data = _load()
    entries = list(_read_guild_players(data, guild_id).values())
    entries.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return entries[:top_n]


def get_streak_leaderboard(top_n: int = 10, *, guild_id: int) -> list[dict]:
    """Return top_n players in the guild sorted by current_streak (streak > 0 only)."""
    data = _load()
    entries = [
        e for e in _read_guild_players(data, guild_id).values()
        if e.get("current_streak", 0) > 0
    ]
    entries.sort(key=lambda x: x.get("current_streak", 0), reverse=True)
    return entries[:top_n]


def get_player_stats(player_id: int, *, guild_id: int) -> dict | None:
    """Return full stats for a player in this guild, or None if not found."""
    data = _load()
    return _read_guild_players(data, guild_id).get(str(player_id))
