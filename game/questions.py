"""
Question loader — reads CSV files from data/questions/ and returns question dicts.
"""

import csv
import logging
import os
import random
from pathlib import Path

logger = logging.getLogger("sigmocatclash.questions")

QUESTIONS_DIR = Path(__file__).parent.parent / "data" / "questions"

_DIFFICULTY_FILES = {
    "easy": ["easy.csv"],
    "medium": ["medium.csv"],
    "hard": ["hard.csv"],
    "all": ["easy.csv", "medium.csv", "hard.csv"],
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
]


def _category_emoji(category: str) -> str:
    cat_lower = category.lower()
    for keyword, emoji in _CATEGORY_EMOJIS:
        if keyword in cat_lower:
            return emoji
    return "📂"


def load_questions(difficulty: str = "all") -> list[dict]:
    questions: list[dict] = []
    filenames = _DIFFICULTY_FILES.get(difficulty, _DIFFICULTY_FILES["all"])

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
                        continue  # skip malformed rows

                    answers = [a.strip() for a in raw_answers.split("|") if a.strip()]
                    if not answers:
                        continue

                    questions.append({
                        "id": row.get("id", "").strip(),
                        "category": category,
                        "letter": letter,
                        "answers": answers,
                        "difficulty": (row.get("difficulty") or difficulty).strip(),
                        "time_limit": _safe_int(row.get("time_limit"), 60),
                        "hint": (row.get("hint") or "").strip(),
                        "emoji": _category_emoji(category),
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
) -> list[dict]:
    """Return up to `count` shuffled questions, skipping already-used IDs."""
    pool = load_questions(difficulty)
    if exclude_ids:
        filtered = [q for q in pool if q["id"] not in exclude_ids]
        pool = filtered if filtered else pool  # reset if pool exhausted

    random.shuffle(pool)
    return pool[:count]


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default
