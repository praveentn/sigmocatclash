#!/usr/bin/env python3
"""
SigmoCatClash — Question Generator
====================================
Generates category-clash questions using Ollama (local) or the Anthropic API,
then appends them to the appropriate CSV file in data/questions/.

Usage examples
--------------
# Interactive mode (prompts you for categories + letters)
  python scripts/generate_questions.py --interactive

# Single category via command line
  python scripts/generate_questions.py --category "Things in a kitchen" --letter B --difficulty easy

# Bulk: pass several categories at once (format: "Category:Letter")
  python scripts/generate_questions.py --bulk "Kitchen items:B" "Sports:F" "Animals:T"

# Choose provider  (default: ollama)
  python scripts/generate_questions.py --provider anthropic --category "Movies" --letter S

Provider setup
--------------
Ollama (local):
  Install Ollama from https://ollama.ai, then run:
    ollama pull llama3.2
  Ensure the Ollama server is running before executing this script.

Anthropic API:
  Set ANTHROPIC_API_KEY in your .env or shell environment.
  pip install -r scripts/requirements_gen.txt
"""

import argparse
import csv
import json
import logging
import os
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("generate_questions")

QUESTIONS_DIR = Path(__file__).parent.parent / "data" / "questions"
QUESTIONS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_MODEL_OLLAMA    = "qwen3:4b"
DEFAULT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"

PROMPT_TEMPLATE = """\
Generate exactly {count} items that belong to the category "{category}" \
and start with the letter "{letter}".

Rules:
- Every item MUST start with the letter {letter} (case-insensitive)
- Items should be real, commonly recognised things
- Difficulty level: {difficulty}  \
(easy = everyday items, medium = moderately known, hard = specific/expert)
- No explanations, no numbering — return ONLY a valid JSON array of strings
- Example output format: ["Bowl", "Bread", "Butter"]

JSON array:"""


# ── AI providers ───────────────────────────────────────────────────────────────

def _extract_json_array(text: str) -> list[str]:
    """Extract the first JSON array from LLM output."""
    start = text.find("[")
    end   = text.rfind("]") + 1
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(text[start:end])
        return [str(i).strip() for i in items if isinstance(i, str) and i.strip()]
    except json.JSONDecodeError:
        return []


def _filter_by_letter(items: list[str], letter: str) -> list[str]:
    return [i for i in items if i.lower().startswith(letter.lower())]


def generate_with_ollama(
    category: str,
    letter: str,
    difficulty: str,
    count: int = 12,
    model: str = DEFAULT_MODEL_OLLAMA,
) -> list[str]:
    try:
        import ollama
    except ImportError:
        log.error("ollama package not installed.  Run: pip install ollama")
        sys.exit(1)

    prompt = PROMPT_TEMPLATE.format(
        category=category, letter=letter, difficulty=difficulty, count=count
    )
    try:
        response = ollama.generate(model=model, prompt=prompt)
        raw = response.get("response", "")
        items = _extract_json_array(raw)
        return _filter_by_letter(items, letter)
    except Exception as exc:
        log.error("Ollama error: %s", exc)
        return []


def generate_with_anthropic(
    category: str,
    letter: str,
    difficulty: str,
    count: int = 12,
    model: str = DEFAULT_MODEL_ANTHROPIC,
) -> list[str]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set in environment / .env")
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        log.error("anthropic package not installed.  Run: pip install anthropic")
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = PROMPT_TEMPLATE.format(
        category=category, letter=letter, difficulty=difficulty, count=count
    )
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        items = _extract_json_array(raw)
        return _filter_by_letter(items, letter)
    except Exception as exc:
        log.error("Anthropic error: %s", exc)
        return []


# ── CSV helpers ────────────────────────────────────────────────────────────────

def _load_existing(filepath: Path) -> tuple[list[dict], int]:
    """Return (rows, max_id)."""
    if not filepath.exists():
        return [], 0
    rows: list[dict] = []
    max_id = 0
    with open(filepath, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
            try:
                max_id = max(max_id, int(row.get("id", 0)))
            except (TypeError, ValueError):
                pass
    return rows, max_id


def _save(filepath: Path, rows: list[dict]) -> None:
    fieldnames = ["id", "category", "letter", "answers", "difficulty", "time_limit", "hint"]
    with open(filepath, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# ── Core generation function ───────────────────────────────────────────────────

def generate_and_save(
    provider: str,
    entries: list[dict],  # list of {category, letter, hint?, time_limit?}
    difficulty: str,
    answer_count: int = 12,
) -> int:
    """Generate answers for each entry and save to CSV. Returns count of new rows."""
    output_file = QUESTIONS_DIR / f"{difficulty}.csv"
    existing_rows, next_id = _load_existing(output_file)
    next_id += 1

    new_rows: list[dict] = []

    for entry in entries:
        category   = entry["category"]
        letter     = entry["letter"].upper()
        hint       = entry.get("hint", "")
        time_limit = entry.get("time_limit", 60)

        label = f"[{next_id + len(new_rows)}] '{category}' → {letter}"
        log.info("Generating %-60s ...", label)

        if provider == "anthropic":
            answers = generate_with_anthropic(category, letter, difficulty, count=answer_count)
        else:
            answers = generate_with_ollama(category, letter, difficulty, count=answer_count)

        if not answers:
            log.warning("  ⚠  No answers returned — skipping.")
            continue

        log.info("  ✓  %d answers: %s", len(answers), ", ".join(answers[:5]) + ("…" if len(answers) > 5 else ""))

        new_rows.append({
            "id":         next_id + len(new_rows) - 1,
            "category":   category,
            "letter":     letter,
            "answers":    "|".join(answers),
            "difficulty": difficulty,
            "time_limit": time_limit,
            "hint":       hint,
        })

    if new_rows:
        _save(output_file, existing_rows + new_rows)
        log.info("✅  Added %d question(s) to %s", len(new_rows), output_file)
    else:
        log.warning("No new questions were generated.")

    return len(new_rows)


# ── CLI modes ──────────────────────────────────────────────────────────────────

def run_interactive(provider: str) -> None:
    print("\n🐱⚡  SigmoCatClash — Question Generator")
    print("=" * 42)
    difficulty = input("Difficulty (easy / medium / hard) [medium]: ").strip().lower() or "medium"
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    print(f"Difficulty set to: {difficulty}\n")

    entries: list[dict] = []
    print("Enter categories (blank line to finish).")
    print("Format:  Category Name, Letter  (e.g.  Things in a kitchen, B)\n")

    while True:
        line = input("> ").strip()
        if not line:
            break
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[1].isalpha() or len(parts[1]) != 1:
            print("  ⚠  Format must be: 'Category, Letter'")
            continue
        entries.append({"category": parts[0], "letter": parts[1].upper()})
        print(f"  Added: {parts[0]} → {parts[1].upper()}")

    if not entries:
        print("Nothing to generate.")
        return

    generate_and_save(provider, entries, difficulty)


def run_bulk(provider: str, difficulty: str, raw_entries: list[str]) -> None:
    entries: list[dict] = []
    for raw in raw_entries:
        if ":" not in raw:
            log.warning("Skipping '%s' — expected format 'Category:Letter'", raw)
            continue
        cat, letter = raw.rsplit(":", 1)
        letter = letter.strip().upper()
        if not letter.isalpha() or len(letter) != 1:
            log.warning("Skipping '%s' — invalid letter '%s'", raw, letter)
            continue
        entries.append({"category": cat.strip(), "letter": letter})

    if not entries:
        log.error("No valid entries to process.")
        sys.exit(1)

    generate_and_save(provider, entries, difficulty)


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SigmoCatClash Question Generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--provider", choices=["ollama", "anthropic"], default="ollama",
        help="AI provider (default: ollama)",
    )
    parser.add_argument(
        "--difficulty", choices=["easy", "medium", "hard"], default="medium",
        help="Question difficulty (default: medium)",
    )
    parser.add_argument("--interactive", action="store_true", help="Interactive mode")
    parser.add_argument(
        "--bulk", nargs="+", metavar="CATEGORY:LETTER",
        help="Generate questions for listed categories (format: 'Kitchen things:B')",
    )
    parser.add_argument("--category", help="Single category name")
    parser.add_argument("--letter",   help="Single letter constraint")
    parser.add_argument("--hint",     default="", help="Optional hint for single-category mode")
    parser.add_argument(
        "--count", type=int, default=12,
        help="Number of answers to generate per question (default: 12)",
    )

    args = parser.parse_args()

    if args.interactive:
        run_interactive(args.provider)

    elif args.bulk:
        run_bulk(args.provider, args.difficulty, args.bulk)

    elif args.category and args.letter:
        if not args.letter.isalpha() or len(args.letter) != 1:
            log.error("--letter must be a single alphabet character.")
            sys.exit(1)
        entry = {
            "category": args.category,
            "letter":   args.letter.upper(),
            "hint":     args.hint,
        }
        generate_and_save(args.provider, [entry], args.difficulty, answer_count=args.count)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
