#!/usr/bin/env python3
"""
SigmoCatClash - Bulk Question Generator
========================================
Generates large batches of questions using Claude Sonnet via the Anthropic API.

MODE 1 - Direct bulk (fastest, one API call generates ~50 complete questions):

  # Add questions until each difficulty has >= 200 total:
  python scripts/bulk_generate.py --target 200

  # One difficulty only:
  python scripts/bulk_generate.py --target 200 --diff medium

  # Custom themed prompt (India / Kerala / World):
  python scripts/bulk_generate.py --target 60 --prompt-file prompts/india_kerala_world.txt --diff medium

MODE 2 - Auto (two-phase: generate categories first, then answers per category):

  # Generate 50 new categories per difficulty + 10 answers each:
  python scripts/bulk_generate.py --auto --categories-per-diff 50 --answers-per-cat 10

  # Reuse saved categories to generate/extend answers:
  python scripts/bulk_generate.py --auto --use-saved --answers-per-cat 12

  # One difficulty:
  python scripts/bulk_generate.py --auto --diff easy --categories-per-diff 30

  # Save categories only, generate questions later:
  python scripts/bulk_generate.py --auto --save-categories-only --categories-per-diff 50

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

QUESTIONS_DIR   = Path(__file__).parent.parent / "data" / "questions"
PROMPTS_DIR     = Path(__file__).parent.parent / "prompts"
CATEGORIES_FILE = Path(__file__).parent.parent / "data" / "categories.json"
MODEL           = "claude-sonnet-4-6"
FIELDNAMES      = ["id", "category", "letter", "answers", "difficulty", "time_limit", "hint"]
MAX_BATCH       = 50

DEFAULT_BULK_PROMPT       = PROMPTS_DIR / "bulk_questions.txt"
DEFAULT_CATEGORIES_PROMPT = PROMPTS_DIR / "categories.txt"
DEFAULT_ANSWERS_PROMPT    = PROMPTS_DIR / "answers.txt"


# ── Prompt loading ────────────────────────────────────────────────────────────

def load_prompt(path: Path) -> str:
    if not path.exists():
        log.error("Prompt file not found: %s", path)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def _resolve(path_arg, default: Path) -> Path:
    if path_arg is None:
        return default
    p = Path(path_arg)
    return p if p.is_absolute() else Path(__file__).parent.parent / p


# ── Saved-categories store ────────────────────────────────────────────────────

def load_saved_categories() -> list[dict]:
    """Load all saved categories from data/categories.json."""
    if not CATEGORIES_FILE.exists():
        return []
    try:
        return json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read categories.json — treating as empty.")
        return []


def save_categories(new_cats: list[dict]) -> int:
    """Merge new categories into data/categories.json. Returns count of actually-new entries."""
    existing = load_saved_categories()
    existing_keys = {
        f"{c['category'].strip().lower()}:{c['letter'].strip().upper()}"
        for c in existing
    }
    added = 0
    for cat in new_cats:
        key = f"{cat['category'].strip().lower()}:{cat['letter'].strip().upper()}"
        if key not in existing_keys:
            existing.append(cat)
            existing_keys.add(key)
            added += 1
    CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    CATEGORIES_FILE.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    return added


# ── CSV helpers ───────────────────────────────────────────────────────────────

def load_csv(filepath: Path) -> tuple[list[dict], int, set[str]]:
    """Return (rows, max_id, existing category:letter pairs)."""
    if not filepath.exists():
        return [], 0, set()
    rows, max_id, existing = [], 0, set()
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
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


# ── Validation helpers ────────────────────────────────────────────────────────

def _valid_word_count(answer: str) -> bool:
    return len(answer.split()) <= 3


# ── Claude API calls ──────────────────────────────────────────────────────────

def _call(client, prompt: str, max_tokens: int = 16000) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def _parse_json_array(raw: str) -> list:
    start, end = raw.find("["), raw.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    try:
        return json.loads(raw[start:end])
    except json.JSONDecodeError:
        return []


def generate_categories_claude(
    client, difficulty: str, count: int, categories_template: str,
) -> list[dict]:
    """Call Claude to generate category+letter pairs. Returns [{category, letter}, ...]."""
    prompt = categories_template.format(count=count, difficulty=difficulty)
    log.info("  Generating %d categories for %s…", count, difficulty)
    raw = _call(client, prompt, max_tokens=2048)
    pairs = _parse_json_array(raw)
    result = []
    for p in pairs:
        if not isinstance(p, dict):
            continue
        cat    = str(p.get("category", "")).strip()
        letter = str(p.get("letter", "")).strip().upper()
        if not cat or not letter:
            continue
        if letter != "*" and (len(letter) != 1 or not letter.isalpha()):
            continue
        result.append({"category": cat, "letter": letter, "difficulty": difficulty})
    return result


def generate_answers_claude(
    client, category: str, letter: str, difficulty: str, count: int, answers_template: str,
) -> list[str]:
    """Call Claude to generate answers for one category+letter pair."""
    if letter == "*":
        prompt = (
            f'Generate exactly {count} well-known items that belong to the category "{category}".\n'
            f'Difficulty: {difficulty} (easy = everyday, medium = general knowledge, hard = expert).\n'
            "Each item must be 1–2 words (3 words maximum in rare cases). No 4+ word answers.\n"
            "Return ONLY a valid JSON array of strings. Example: [\"Item One\", \"Item Two\"]"
        )
    else:
        prompt = answers_template.format(
            category=category, letter=letter, difficulty=difficulty, count=count
        )
    raw = _call(client, prompt, max_tokens=1024)
    items = _parse_json_array(raw)
    answers = [str(i).strip() for i in items if isinstance(i, str) and str(i).strip()]
    if letter != "*":
        answers = [a for a in answers if a.lower().startswith(letter.lower())]
    return [a for a in answers if _valid_word_count(a)]


# ── Bulk (mode 1) ─────────────────────────────────────────────────────────────

def _bulk_call_claude(client, difficulty: str, count: int, avoid_pairs: set[str], bulk_template: str) -> list[dict]:
    avoid_block = "\n".join(f"  - {p}" for p in sorted(avoid_pairs)) or "  (none)"
    prompt = bulk_template.format(count=count, difficulty=difficulty, avoid_block=avoid_block)
    log.info("  Calling Claude %s for %d questions (difficulty=%s)…", MODEL, count, difficulty)
    raw = _call(client, prompt)
    return _parse_json_array(raw)


def _validate_bulk(questions: list) -> list[dict]:
    clean = []
    for q in questions:
        if not isinstance(q, dict):
            continue
        cat    = str(q.get("category", "")).strip()
        letter = str(q.get("letter", "")).strip().upper()
        raw_ans = q.get("answers", [])
        hint   = str(q.get("hint", "")).strip()
        try:
            time_limit = int(q.get("time_limit", 60))
        except (TypeError, ValueError):
            time_limit = 60

        if not cat or not letter:
            continue
        if letter != "*" and (len(letter) != 1 or not letter.isalpha()):
            continue
        if not isinstance(raw_ans, list):
            continue

        if letter == "*":
            valid_ans = [str(a).strip() for a in raw_ans
                         if isinstance(a, str) and str(a).strip() and _valid_word_count(str(a).strip())]
        else:
            valid_ans = [str(a).strip() for a in raw_ans
                         if isinstance(a, str)
                         and str(a).strip().lower().startswith(letter.lower())
                         and _valid_word_count(str(a).strip())]

        if len(valid_ans) < 3:
            hint_fix = " (use '*' for people/actors/politicians)" if len(valid_ans) < 3 else " (pick a letter with more answers)"
            log.warning("  Skipping '%s'/%s — only %d valid answers%s", cat, letter, len(valid_ans), hint_fix)
            continue

        clean.append({"category": cat, "letter": letter, "answers": valid_ans,
                      "hint": hint, "time_limit": time_limit})
    return clean


def run_bulk(client, difficulty: str, target: int, bulk_template: str) -> int:
    filepath = QUESTIONS_DIR / f"{difficulty}.csv"
    existing_rows, max_id, existing_pairs = load_csv(filepath)
    current = len(existing_rows)

    if current >= target:
        log.info("[%s] Already has %d/%d — skipping.", difficulty, current, target)
        return 0

    needed    = target - current
    new_rows: list[dict] = []
    all_pairs = set(existing_pairs)
    remaining = needed
    log.info("[%s] Has %d, need %d more (target %d).", difficulty, current, needed, target)

    while remaining > 0:
        batch     = min(remaining + 10, MAX_BATCH)
        raw       = _bulk_call_claude(client, difficulty, batch, all_pairs, bulk_template)
        validated = _validate_bulk(raw)

        added = 0
        for q in validated:
            key = f"{q['category'].lower()}:{q['letter'].upper()}"
            if key in all_pairs:
                continue
            all_pairs.add(key)
            max_id += 1
            new_rows.append({
                "id": max_id, "category": q["category"], "letter": q["letter"],
                "answers": "|".join(q["answers"]), "difficulty": difficulty,
                "time_limit": q["time_limit"], "hint": q["hint"],
            })
            remaining -= 1
            added += 1
            if remaining <= 0:
                break

        log.info("  Batch: %d/%d accepted.", added, len(validated))
        if added == 0:
            log.warning("[%s] 0 usable questions — stopping early.", difficulty)
            break

    if new_rows:
        save_csv(filepath, existing_rows + new_rows)
        log.info("[%s] Saved. Added %d → total %d.", difficulty, len(new_rows), current + len(new_rows))
    return len(new_rows)


# ── Auto (mode 2) ─────────────────────────────────────────────────────────────

def run_auto(
    client,
    difficulties: list[str],
    categories_per_diff: int,
    answers_per_cat: int,
    categories_template: str,
    answers_template: str,
    use_saved: bool,
    save_only: bool,
) -> int:
    """
    Two-phase generation:
      Phase 1 — generate (or load) category+letter pairs, save to data/categories.json
      Phase 2 — for each new category, generate answers and append to the CSV
    """
    # ── Phase 1: get categories ───────────────────────────────────
    if use_saved:
        all_saved = load_saved_categories()
        cats_by_diff: dict[str, list[dict]] = {}
        for c in all_saved:
            diff = c.get("difficulty", "medium")
            if diff in difficulties:
                cats_by_diff.setdefault(diff, []).append(c)
        log.info("Loaded %d saved categories from %s.", len(all_saved), CATEGORIES_FILE)
    else:
        cats_by_diff = {}
        all_new: list[dict] = []
        for diff in difficulties:
            cats = generate_categories_claude(client, diff, categories_per_diff, categories_template)
            cats_by_diff[diff] = cats
            all_new.extend(cats)
        added_to_file = save_categories(all_new)
        log.info("Saved %d new categories (%d total in file).",
                 added_to_file, len(load_saved_categories()))

    if save_only:
        log.info("--save-categories-only: stopping after phase 1.")
        return 0

    # ── Phase 2: generate questions for each category ─────────────
    total_added = 0
    for diff in difficulties:
        cats = cats_by_diff.get(diff, [])
        if not cats:
            log.warning("[%s] No categories to generate for.", diff)
            continue

        filepath = QUESTIONS_DIR / f"{diff}.csv"
        existing_rows, max_id, existing_pairs = load_csv(filepath)
        new_rows: list[dict] = []

        log.info("[%s] Generating questions for %d categories…", diff, len(cats))
        for cat in cats:
            category = cat["category"]
            letter   = cat["letter"]
            key      = f"{category.lower()}:{letter.upper()}"
            if key in existing_pairs:
                log.info("  Already exists: %s / %s — skipping.", category, letter)
                continue

            log.info("  %s → %s", category, letter)
            answers = generate_answers_claude(
                client, category, letter, diff, answers_per_cat, answers_template
            )
            if len(answers) < 3:
                hint_fix = " (use '*' for people/actors/politicians)" if letter != "*" and len(answers) < 3 else ""
                log.warning("  Only %d answers for '%s'/%s — skipping.%s", len(answers), category, letter, hint_fix)
                continue

            max_id += 1
            new_rows.append({
                "id": max_id, "category": category, "letter": letter,
                "answers": "|".join(answers), "difficulty": diff,
                "time_limit": 60, "hint": "",
            })
            existing_pairs.add(key)

        if new_rows:
            save_csv(filepath, existing_rows + new_rows)
            log.info("[%s] Saved %d new questions.", diff, len(new_rows))
            total_added += len(new_rows)

    return total_added


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk question generator for SigmoCatClash",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── shared ──────────────────────────────────────────────────────────────
    parser.add_argument("--diff", choices=["easy", "medium", "hard"],
                        help="Run one difficulty only (default: all three)")

    # ── mode 1: direct bulk ──────────────────────────────────────────────────
    parser.add_argument("--target", type=int, default=None,
                        help="[mode 1] Target total questions per difficulty (e.g. 200)")
    parser.add_argument("--prompt-file", type=Path, default=None,
                        help="[mode 1] Bulk prompt template (default: prompts/bulk_questions.txt)")

    # ── mode 2: auto ─────────────────────────────────────────────────────────
    parser.add_argument("--auto", action="store_true",
                        help="[mode 2] Two-phase: generate categories then answers separately")
    parser.add_argument("--categories-per-diff", type=int, default=50,
                        help="[mode 2] New categories to generate per difficulty (default: 50)")
    parser.add_argument("--answers-per-cat", type=int, default=10,
                        help="[mode 2] Answers to generate per category (default: 10)")
    parser.add_argument("--use-saved", action="store_true",
                        help="[mode 2] Use categories from data/categories.json instead of generating new ones")
    parser.add_argument("--save-categories-only", action="store_true",
                        help="[mode 2] Only save categories to data/categories.json; don't generate questions")
    parser.add_argument("--categories-prompt", type=Path, default=None,
                        help="[mode 2] Categories prompt file (default: prompts/categories.txt)")
    parser.add_argument("--answers-prompt", type=Path, default=None,
                        help="[mode 2] Answers prompt file (default: prompts/answers.txt)")

    args = parser.parse_args()

    # Must pick a mode
    if not args.auto and args.target is None:
        parser.print_help()
        print("\nError: specify --target N (mode 1) or --auto (mode 2)")
        sys.exit(1)

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set. Add it to your .env file.")
        sys.exit(1)
    try:
        import anthropic
    except ImportError:
        log.error("anthropic not installed. Run: pip install anthropic")
        sys.exit(1)

    client        = anthropic.Anthropic(api_key=api_key)
    difficulties  = [args.diff] if args.diff else ["easy", "medium", "hard"]

    if args.auto:
        categories_prompt = load_prompt(_resolve(args.categories_prompt, DEFAULT_CATEGORIES_PROMPT))
        answers_prompt    = load_prompt(_resolve(args.answers_prompt, DEFAULT_ANSWERS_PROMPT))
        log.info("Mode: AUTO  |  categories-per-diff=%d  answers-per-cat=%d  use-saved=%s",
                 args.categories_per_diff, args.answers_per_cat, args.use_saved)
        total = run_auto(
            client,
            difficulties=difficulties,
            categories_per_diff=args.categories_per_diff,
            answers_per_cat=args.answers_per_cat,
            categories_template=categories_prompt,
            answers_template=answers_prompt,
            use_saved=args.use_saved,
            save_only=args.save_categories_only,
        )
        log.info("Done. %d new questions added.", total)

    else:
        bulk_prompt = load_prompt(_resolve(args.prompt_file, DEFAULT_BULK_PROMPT))
        log.info("Mode: BULK  |  target=%d  difficulties=%s", args.target, difficulties)
        total = 0
        for diff in difficulties:
            total += run_bulk(client, diff, target=args.target, bulk_template=bulk_prompt)
        log.info("Done. %d new questions added.", total)


if __name__ == "__main__":
    main()
