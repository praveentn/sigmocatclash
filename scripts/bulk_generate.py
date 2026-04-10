#!/usr/bin/env python3
"""
SigmoCatClash — Bulk Question Generator
========================================
Generates large batches of questions using Claude Sonnet via the Anthropic API.
Makes ~2 API calls per difficulty level (much more efficient than one-by-one).

Usage:
  python scripts/bulk_generate.py              # fills all difficulties to 100 questions
  python scripts/bulk_generate.py --target 150 # set a different target
  python scripts/bulk_generate.py --diff easy  # only one difficulty

Setup:
  Add ANTHROPIC_API_KEY=sk-ant-... to your .env file, then run.
"""

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("bulk_generate")

QUESTIONS_DIR = Path(__file__).parent.parent / "data" / "questions"
MODEL = "claude-sonnet-4-6"
FIELDNAMES = ["id", "category", "letter", "answers", "difficulty", "time_limit", "hint"]
MAX_BATCH = 50   # questions per single API call


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_csv(filepath: Path) -> tuple[list[dict], int, set[str]]:
    """Return (rows, max_id, existing category:letter pairs)."""
    if not filepath.exists():
        return [], 0, set()
    rows: list[dict] = []
    max_id = 0
    existing: set[str] = set()
    with open(filepath, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(row)
            try:
                max_id = max(max_id, int(row.get("id", 0)))
            except (TypeError, ValueError):
                pass
            key = f"{row.get('category', '').strip().lower()}:{row.get('letter', '').strip().upper()}"
            existing.add(key)
    return rows, max_id, existing


def save_csv(filepath: Path, rows: list[dict]) -> None:
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── Generation ────────────────────────────────────────────────────────────────

PROMPT_TEMPLATE = """\
You are writing trivia questions for SigmoCatClash — a fast-paced Discord word game.
Players are shown a category (e.g. "Things in a kitchen") and a starting letter (e.g. "B"),
then race to type as many matching items as they can within 60 seconds.

Generate exactly {count} UNIQUE questions for difficulty: **{difficulty}**

Difficulty guide:
  easy   — everyday objects, common animals/foods/colors, sports, school items, household things
  medium — geography, movies/TV, cooking techniques, science, technology, board games, music genres
  hard   — chemical elements, philosophical terms, medical/anatomy, advanced math, specific history,
           programming, linguistics, opera, economic theories, rare species

Rules for each question:
  1. "category" — clear, fun topic description (e.g. "Types of cheese", "Things at an airport")
  2. "letter"   — a single capital letter. Pick letters with MANY valid answers (avoid X, Q, Z unless hard).
  3. "answers"  — array of 10–15 items that ALL start with the given letter (case-insensitive).
                  Include a mix of obvious AND slightly less obvious items.
                  TRIPLE-CHECK every answer starts with the letter — no exceptions.
  4. "hint"     — short helpful clue string, or "" if not needed
  5. "time_limit" — integer seconds. Use 60 for almost everything.
                    Use 10–15 ONLY for questions with very few possible answers (e.g. logic gates).

Avoid these category+letter combinations (already exist):
{avoid_block}

Return ONLY a valid JSON array — no markdown, no explanation, no code fences.

[
  {{
    "category": "Things in a kitchen",
    "letter": "B",
    "answers": ["Bowl", "Bread", "Butter", "Blender", "Baking sheet", "Broiler", "Biscuit"],
    "hint": "",
    "time_limit": 60
  }},
  ...
]
"""


def _call_claude(client, difficulty: str, count: int, avoid_pairs: set[str]) -> list[dict]:
    """Single Anthropic API call — returns raw parsed JSON list."""
    avoid_block = "\n".join(f"  - {p}" for p in sorted(avoid_pairs)) or "  (none)"
    prompt = PROMPT_TEMPLATE.format(
        count=count,
        difficulty=difficulty,
        avoid_block=avoid_block,
    )

    log.info("  Calling Claude %s for %d %s questions…", MODEL, count, difficulty)
    response = client.messages.create(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()

    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start < 0 or end <= start:
        log.error("  No JSON array in response. Raw snippet: %s", raw[:300])
        return []
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError as exc:
        log.error("  JSON parse error: %s", exc)
        return []


def _validate(questions: list, letter_required: str = "") -> list[dict]:
    """Filter and clean raw question dicts. Returns only valid ones."""
    clean = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        cat = str(q.get("category", "")).strip()
        letter = str(q.get("letter", "")).strip().upper()
        raw_ans = q.get("answers", [])
        hint = str(q.get("hint", "")).strip()
        try:
            time_limit = int(q.get("time_limit", 60))
        except (TypeError, ValueError):
            time_limit = 60

        if not cat or not letter or len(letter) != 1 or not letter.isalpha():
            continue
        if letter_required and letter != letter_required.upper():
            continue
        if not isinstance(raw_ans, list):
            continue

        # Keep only answers that genuinely start with the letter
        valid_ans = [
            str(a).strip()
            for a in raw_ans
            if isinstance(a, str)
            and str(a).strip().lower().startswith(letter.lower())
        ]

        if len(valid_ans) < 4:
            log.warning("  Skipping '%s'/%s — only %d valid answers", cat, letter, len(valid_ans))
            continue

        clean.append({
            "category": cat,
            "letter": letter,
            "answers": valid_ans,
            "hint": hint,
            "time_limit": time_limit,
        })
    return clean


# ── Per-difficulty runner ─────────────────────────────────────────────────────

def run_difficulty(client, difficulty: str, target: int) -> int:
    filepath = QUESTIONS_DIR / f"{difficulty}.csv"
    existing_rows, max_id, existing_pairs = load_csv(filepath)
    current = len(existing_rows)

    if current >= target:
        log.info("[%s] Already has %d/%d questions — skipping.", difficulty, current, target)
        return 0

    needed = target - current
    log.info("[%s] Has %d, need %d more (target %d).", difficulty, current, needed, target)

    new_rows: list[dict] = []
    all_pairs = set(existing_pairs)
    remaining = needed

    while remaining > 0:
        batch = min(remaining + 10, MAX_BATCH)  # ask for a few extra to cover invalids
        raw = _call_claude(client, difficulty, batch, all_pairs)
        validated = _validate(raw)

        added_this_batch = 0
        for q in validated:
            key = f"{q['category'].lower()}:{q['letter'].upper()}"
            if key in all_pairs:
                log.info("  Skip duplicate: %s / %s", q["category"], q["letter"])
                continue
            all_pairs.add(key)
            max_id += 1
            new_rows.append({
                "id":         max_id,
                "category":   q["category"],
                "letter":     q["letter"],
                "answers":    "|".join(q["answers"]),
                "difficulty": difficulty,
                "time_limit": q["time_limit"],
                "hint":       q["hint"],
            })
            remaining -= 1
            added_this_batch += 1
            if remaining <= 0:
                break

        log.info("  Batch result: %d/%d accepted.", added_this_batch, len(validated))

        # If Claude keeps returning too few valid items, stop to avoid infinite loop
        if added_this_batch == 0:
            log.warning("[%s] Got 0 usable questions — stopping early.", difficulty)
            break

    if new_rows:
        save_csv(filepath, existing_rows + new_rows)
        log.info("[%s] Saved. Added %d → total %d.", difficulty, len(new_rows), current + len(new_rows))
    return len(new_rows)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Bulk question generator for SigmoCatClash")
    parser.add_argument("--target", type=int, default=100, help="Target question count per difficulty (default: 100)")
    parser.add_argument("--diff", choices=["easy", "medium", "hard"], help="Only run one difficulty")
    args = parser.parse_args()

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error(
            "ANTHROPIC_API_KEY is not set.\n"
            "  Add it to your .env file:\n"
            "    ANTHROPIC_API_KEY=sk-ant-...\n"
            "  Then re-run this script."
        )
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        log.error("anthropic not installed. Run: pip install anthropic")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    log.info("Model: %s  |  Target: %d per difficulty", MODEL, args.target)

    difficulties = [args.diff] if args.diff else ["easy", "medium", "hard"]
    total = 0
    for diff in difficulties:
        added = run_difficulty(client, diff, target=args.target)
        total += added

    log.info("Done. %d new questions added across all difficulties.", total)


if __name__ == "__main__":
    main()
