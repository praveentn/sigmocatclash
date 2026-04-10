"""
Persistent overall leaderboard — accumulates scores across all completed games.

Stored as JSON at data/overall_leaderboard.json. Keys are player IDs (strings)
so they survive display-name changes.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("sigmocatclash.leaderboard")

LEADERBOARD_PATH = Path(__file__).parent.parent / "data" / "overall_leaderboard.json"


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


def record_game_results(player_scores: dict) -> None:
    """
    Persist results from a completed game into the overall leaderboard.

    player_scores: {player_id (int): (display_name (str), score (int))}
    """
    if not player_scores:
        return

    data = _load()
    players = data.setdefault("players", {})

    for player_id, (name, score) in player_scores.items():
        pid_str = str(player_id)
        entry = players.setdefault(pid_str, {
            "name": name,
            "total_score": 0,
            "games_played": 0,
            "best_score": 0,
        })
        entry["name"] = name  # update in case of nick change
        entry["total_score"] += score
        entry["games_played"] += 1
        if score > entry["best_score"]:
            entry["best_score"] = score

    _save(data)


def get_overall_leaderboard(top_n: int = 15) -> list:
    """
    Return top_n players sorted by total_score descending.

    Each entry is a dict: {name, total_score, games_played, best_score}
    """
    data = _load()
    entries = list(data.get("players", {}).values())
    entries.sort(key=lambda x: x.get("total_score", 0), reverse=True)
    return entries[:top_n]
