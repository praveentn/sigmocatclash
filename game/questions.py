"""
Question loader — reads CSV files from data/questions/ and returns question dicts.
"""

import csv
import json
import logging
import os
import random
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("sigmocatclash.questions")

QUESTIONS_DIR = Path(__file__).parent.parent / "data" / "questions"
SCHEDULE_FILE = Path(__file__).parent.parent / "data" / "schedule.json"

_DIFFICULTY_FILES = {
    "easy":   ["easy.csv"],
    "medium": ["medium.csv"],
    "hard":   ["hard.csv"],
    "all":    ["easy.csv", "medium.csv", "hard.csv"],
}

# Emoji hints for category keywords
_CATEGORY_EMOJIS: list[tuple[str, str]] = [
    ("kitchen", "🍳"), ("food", "🍔"), ("fruit", "🍎"), ("vegetable", "🥕"),
    ("animal", "🐾"), ("sport", "⚽"), ("music", "🎵"), ("instrument", "🎸"),
    ("movie", "🎬"), ("tv show", "📺"), ("game", "🎮"), ("board game", "🎲"),
    ("country", "🌍"), ("city", "🏙️"), ("capital", "🗺️"),
    ("body", "💪"), ("hospital", "🏥"), ("doctor", "🩺"),
    ("beach", "🏖️"), ("vacation", "✈️"), ("airport", "🛫"),
    ("space", "🚀"), ("planet", "🪐"), ("science", "🔬"),
    ("clothes", "👕"), ("wear", "👗"), ("color", "🎨"),
    ("living room", "🛋️"), ("bedroom", "🛏️"), ("toolbox", "🔧"),
    ("gym", "🏋️"), ("superhero", "🦸"), ("cheese", "🧀"),
    ("painter", "🖌️"), ("philosopher", "💭"), ("element", "⚗️"),
    ("dinosaur", "🦕"), ("cloud", "☁️"), ("math", "📐"),
    ("currency", "💰"), ("opera", "🎭"), ("composer", "🎼"),
    # India / Kerala / World additions
    ("india", "🇮🇳"), ("kerala", "🌴"), ("river", "🏞️"),
    ("dam", "💧"), ("state", "🗾"), ("monument", "🏛️"),
    ("bollywood", "🎥"), ("actor", "🎭"), ("director", "🎬"),
    ("hill station", "⛰️"), ("mountain", "🏔️"),
    ("viceroy", "👑"), ("chief minister", "🏛️"),
    ("world", "🌐"), ("chocolate", "🍫"), ("restaurant", "🍽️"),
    ("car", "🚗"),
]


def _category_emoji(category: str) -> str:
    cat_lower = category.lower()
    for keyword, emoji in _CATEGORY_EMOJIS:
        if keyword in cat_lower:
            return emoji
    return "📂"


# ── Daily schedule ────────────────────────────────────────────────────────────

def get_daily_pool_key() -> str:
    """
    Return the pool key for today's daily theme (from data/schedule.json).
    Falls back to "all" if the schedule is missing or unreadable.
    """
    if not SCHEDULE_FILE.exists():
        return "all"
    try:
        schedule = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        day = datetime.now().strftime("%A").lower()
        return schedule.get(day, "all")
    except Exception:
        return "all"


def _files_for_difficulty(difficulty: str) -> list[str]:
    if difficulty == "daily":
        pool_key = get_daily_pool_key()  # returns a difficulty string from schedule.json
        return _DIFFICULTY_FILES.get(pool_key, _DIFFICULTY_FILES["all"])
    return _DIFFICULTY_FILES.get(difficulty, _DIFFICULTY_FILES["all"])


def resolve_pool_key(difficulty: str) -> str:
    """
    Return the stable pool key used for server-progress tracking.
    'daily' resolves to today's actual difficulty from schedule.json.
    """
    if difficulty == "daily":
        return get_daily_pool_key()   # e.g. "medium" or "all"
    return difficulty


# ── CSV loading ───────────────────────────────────────────────────────────────

def load_questions(difficulty: str = "all") -> list[dict]:
    questions: list[dict] = []
    filenames = _files_for_difficulty(difficulty)

    for filename in filenames:
        filepath = QUESTIONS_DIR / filename
        if not filepath.exists():
            logger.warning("Questions file not found: %s", filepath)
            continue
        try:
            with open(filepath, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    category = (row.get("category") or "").strip()
                    letter = (row.get("letter") or "").strip().upper()
                    raw_answers = (row.get("answers") or "").strip()

                    if not category or not letter or not raw_answers:
                        continue

                    answers = [a.strip() for a in raw_answers.split("|") if a.strip()]
                    if not answers:
                        continue

                    questions.append({
                        "id":         row.get("id", "").strip(),
                        "category":   category,
                        "letter":     letter,
                        "answers":    answers,
                        "difficulty": (row.get("difficulty") or difficulty).strip(),
                        "time_limit": _safe_int(row.get("time_limit"), 60),
                        "hint":       (row.get("hint") or "").strip(),
                        "emoji":      _category_emoji(category),
                        "_pool":      Path(filename).stem,   # track source file
                    })
        except Exception:
            logger.exception("Failed to load %s", filepath)

    if not questions:
        logger.warning("No questions loaded from data/questions/ — add CSV files!")
    return questions


def get_random_questions(
    count: int,
    difficulty: str = "all",
    exclude_ids: set[str] | None = None,
    guild_id: int | None = None,
) -> list[dict]:
    """
    Return up to `count` shuffled questions.

    If guild_id is provided:
    - Questions already asked in this server (for this pool) are excluded.
    - If the entire pool has been played, it resets automatically and a full
      pool is available again (questions start recycling from the beginning).
    """
    pool = load_questions(difficulty)
    pool_key = resolve_pool_key(difficulty)

    if guild_id is not None:
        from game.server_progress import check_and_auto_reset, get_asked_ids
        check_and_auto_reset(guild_id, pool_key, len(pool))
        asked = get_asked_ids(guild_id, pool_key)
        fresh = [q for q in pool if q["id"] not in asked]
        pool = fresh if fresh else pool   # if somehow all exhausted, fallback to full pool

    if exclude_ids:
        filtered = [q for q in pool if q["id"] not in exclude_ids]
        pool = filtered if filtered else pool

    random.shuffle(pool)
    return pool[:count]


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
