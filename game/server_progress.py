"""
server_progress — per-server question tracking.

Persists to data/server_progress.json.
Structure:
  {
    "<guild_id>": {
      "<pool_key>": ["<question_id>", ...]
    }
  }

pool_key examples: "easy", "medium", "hard", "all", "india_kerala_world", "daily"
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("sigmocatclash.server_progress")

_PROGRESS_FILE = Path(__file__).parent.parent / "data" / "server_progress.json"


def _load() -> dict:
    if not _PROGRESS_FILE.exists():
        return {}
    try:
        return json.loads(_PROGRESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read server_progress.json — starting fresh.")
        return {}


def _save(data: dict) -> None:
    _PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _PROGRESS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_asked_ids(guild_id: int, pool_key: str) -> set[str]:
    """Return the set of question IDs already asked in this guild+pool."""
    data = _load()
    return set(data.get(str(guild_id), {}).get(pool_key, []))


def mark_questions_asked(guild_id: int, question_ids: list[str], pool_key: str) -> None:
    """Record that a batch of question IDs were asked in this guild+pool."""
    if not question_ids:
        return
    data = _load()
    guild_key = str(guild_id)
    asked: list = data.setdefault(guild_key, {}).setdefault(pool_key, [])
    added = 0
    for qid in question_ids:
        if qid and qid not in asked:
            asked.append(qid)
            added += 1
    if added:
        _save(data)


def reset_pool(guild_id: int, pool_key: str) -> None:
    """Clear the asked-list for this guild+pool so all questions become available again."""
    data = _load()
    guild_key = str(guild_id)
    if guild_key in data and pool_key in data[guild_key]:
        data[guild_key][pool_key] = []
        _save(data)
        log.info("Guild %s: pool '%s' reset.", guild_id, pool_key)


def check_and_auto_reset(guild_id: int, pool_key: str, total_in_pool: int) -> bool:
    """
    If all questions in the pool have been asked, reset automatically.
    Returns True if a reset occurred.
    """
    if total_in_pool <= 0:
        return False
    asked = get_asked_ids(guild_id, pool_key)
    if len(asked) >= total_in_pool:
        log.info(
            "Guild %s: all %d questions in pool '%s' played — resetting.",
            guild_id, total_in_pool, pool_key,
        )
        reset_pool(guild_id, pool_key)
        return True
    return False


def pool_progress(guild_id: int, pool_key: str, total_in_pool: int) -> tuple[int, int]:
    """Return (asked_count, total_count)."""
    asked = get_asked_ids(guild_id, pool_key)
    return len(asked), total_in_pool
