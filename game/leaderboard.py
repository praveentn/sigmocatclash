"""
Persistent overall leaderboard — accumulates scores across all completed games.

Stored as JSON at data/overall_leaderboard.json. Keys are player IDs (strings)
so they survive display-name changes.

Player entry schema:
  name, total_score, games_played, best_score, wins,
  total_answers, current_streak, longest_streak, last_played, achievements
"""

import json
import logging
from datetime import date
from pathlib import Path

from game.achievements import check_new_achievements

log = logging.getLogger("sigmocatclash.leaderboard")

LEADERBOARD_PATH = Path(__file__).parent.parent / "data" / "overall_leaderboard.json"

# Rank tiers (min_score, display_name, emoji) — ascending order required
RANKS = [
    (0,    "Rookie",  "🪨"),
    (50,   "Player",  "🥉"),
    (150,  "Pro",     "🥈"),
    (350,  "Elite",   "🥇"),
    (700,  "Master",  "💎"),
    (1200, "Legend",  "👑"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load() -> dict:
    if LEADERBOARD_PATH.exists():
        try:
            with open(LEADERBOARD_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            log.error("Failed to load overall leaderboard: %s", exc)
    return {"players": {}}


def _save(data: dict) -> None:
    try:
        LEADERBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LEADERBOARD_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error("Failed to save overall leaderboard: %s", exc)


def get_rank(total_score: int) -> tuple[str, str, int]:
    """
    Return (rank_name, rank_emoji, next_threshold) for a given total_score.
    next_threshold is -1 when at maximum rank.
    """
    rank_name, rank_emoji, rank_idx = RANKS[0][1], RANKS[0][2], 0
    for i, (threshold, name, emoji) in enumerate(RANKS):
        if total_score >= threshold:
            rank_name, rank_emoji, rank_idx = name, emoji, i
        else:
            break
    next_threshold = RANKS[rank_idx + 1][0] if rank_idx + 1 < len(RANKS) else -1
    return rank_name, rank_emoji, next_threshold


def _update_daily_streak(entry: dict, today_str: str) -> None:
    """Update current_streak / longest_streak based on last_played date."""
    last_played = entry.get("last_played", "")

    if last_played == today_str:
        return  # already counted today

    if last_played:
        try:
            delta = (date.fromisoformat(today_str) - date.fromisoformat(last_played)).days
            if delta == 1:
                entry["current_streak"] = entry.get("current_streak", 0) + 1
            else:
                entry["current_streak"] = 1
        except ValueError:
            entry["current_streak"] = 1
    else:
        entry["current_streak"] = 1

    entry["last_played"] = today_str
    if entry["current_streak"] > entry.get("longest_streak", 0):
        entry["longest_streak"] = entry["current_streak"]


# ── Public API ────────────────────────────────────────────────────────────────

def record_game_results(
    player_scores: dict,
    player_answers: dict | None = None,
) -> dict[int, list[str]]:
    """
    Persist results from a completed game into the overall leaderboard.

    player_scores  : {player_id (int): (display_name (str), score (int))}
    player_answers : {player_id (int): int}  — valid answers this game (optional)

    Returns: {player_id (int): [newly_earned_achievement_id, ...]}
    """
    if not player_scores:
        return {}

    data = _load()
    players = data.setdefault("players", {})
    today_str = date.today().isoformat()
    player_answers = player_answers or {}

    # Determine winner(s) — highest score among players who scored > 0
    max_score = max((s for _, s in player_scores.values()), default=0)
    winners = {pid for pid, (_, s) in player_scores.items() if s == max_score and s > 0}

    new_achievements: dict[int, list[str]] = {}

    for player_id, (name, score) in player_scores.items():
        pid_str = str(player_id)
        entry = players.setdefault(pid_str, {
            "name": name,
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
        entry["name"] = name  # keep current display name
        entry["total_score"] = entry.get("total_score", 0) + score
        entry["games_played"] = entry.get("games_played", 0) + 1
        if score > entry.get("best_score", 0):
            entry["best_score"] = score
        if player_id in winners:
            entry["wins"] = entry.get("wins", 0) + 1
        entry["total_answers"] = entry.get("total_answers", 0) + player_answers.get(player_id, 0)

        _update_daily_streak(entry, today_str)

        earned = check_new_achievements(entry)
        if earned:
            new_achievements[player_id] = earned

    _save(data)
    return new_achievements


def get_overall_leaderboard(top_n: int = 15) -> list[dict]:
    """Return top_n players sorted by total_score descending."""
    data = _load()
    entries = list(data.get("players", {}).values())
    entries.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return entries[:top_n]


def get_streak_leaderboard(top_n: int = 10) -> list[dict]:
    """Return top_n players sorted by current_streak descending (streak > 0 only)."""
    data = _load()
    entries = [e for e in data.get("players", {}).values() if e.get("current_streak", 0) > 0]
    entries.sort(key=lambda x: x.get("current_streak", 0), reverse=True)
    return entries[:top_n]


def get_player_stats(player_id: int) -> dict | None:
    """Return full stats dict for a player by Discord ID, or None if not found."""
    data = _load()
    return data.get("players", {}).get(str(player_id))
