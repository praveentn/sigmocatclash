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

# Use a custom categories prompt (to auto-generate category suggestions)
  python scripts/generate_questions.py --auto --categories-prompt prompts/india_kerala_world.txt

# Choose provider  (default: ollama)
  python scripts/generate_questions.py --provider anthropic --category "Movies" --letter S

Provider setup
--------------
Ollama (local):
  Install Ollama from https://ollama.ai, then run:
    ollama pull qwen3.5:4b
  Ensure the Ollama server is running before executing this script.
  Other models work too — pass --model <name> to override.

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

PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

DEFAULT_CATEGORIES_PROMPT_FILE = PROMPTS_DIR / "categories.txt"
DEFAULT_ANSWERS_PROMPT_FILE    = PROMPTS_DIR / "answers.txt"

DEFAULT_MODEL_OLLAMA    = "qwen3.5:4b"
DEFAULT_MODEL_ANTHROPIC = "claude-haiku-4-5-20251001"


# ── Prompt loading ────────────────────────────────────────────────────────────

def load_prompt_template(path: Path) -> str:
    """Load a prompt template from a file."""
    if not path.exists():
        log.error("Prompt file not found: %s", path)
        sys.exit(1)
    return path.read_text(encoding="utf-8")


def _resolve_prompt_path(arg_path: str | None, default: Path) -> Path:
    """Resolve a user-supplied prompt path (relative to project root) or use default."""
    if arg_path is None:
        return default
    p = Path(arg_path)
    if not p.is_absolute():
        p = Path(__file__).parent.parent / p
    return p


# ── AI providers ───────────────────────────────────────────────────────────────

import re as _re

def _strip_thinking(text: str) -> str:
    """Remove <think>…</think> blocks that qwen3 and other reasoning models emit."""
    return _re.sub(r"<think>.*?</think>", "", text, flags=_re.DOTALL).strip()


def _extract_json_array(text: str) -> list[str]:
    """Extract the first JSON array from LLM output, ignoring thinking tokens."""
    text = _strip_thinking(text)
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
    if letter == "*":
        return items
    return [i for i in items if i.lower().startswith(letter.lower())]


def generate_with_ollama(
    category: str,
    letter: str,
    difficulty: str,
    count: int = 12,
    model: str = DEFAULT_MODEL_OLLAMA,
    answers_template: str | None = None,
) -> list[str]:
    try:
        import ollama
    except ImportError:
        log.error("ollama package not installed.  Run: pip install ollama")
        sys.exit(1)

    template = answers_template or load_prompt_template(DEFAULT_ANSWERS_PROMPT_FILE)
    # /no_think suppresses the <think> block on qwen3 and compatible models
    prompt = "/no_think\n" + template.format(
        category=category, letter=letter, difficulty=difficulty, count=count
    )
    try:
        response = ollama.generate(model=model, prompt=prompt)
        # Newer ollama SDK returns an object; older versions return a dict
        raw = response.response if hasattr(response, "response") else response.get("response", "")
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
    answers_template: str | None = None,
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

    template = answers_template or load_prompt_template(DEFAULT_ANSWERS_PROMPT_FILE)
    client = anthropic.Anthropic(api_key=api_key)
    prompt = template.format(
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


# ── Category generation ────────────────────────────────────────────────────────

def _generate_categories(
    provider: str,
    difficulty: str,
    count: int,
    model: str | None,
    categories_template: str | None = None,
) -> list[dict]:
    """Ask the AI to suggest category+letter pairs. Returns [{category, letter}, ...]."""
    template = categories_template or load_prompt_template(DEFAULT_CATEGORIES_PROMPT_FILE)
    prompt = template.format(count=count, difficulty=difficulty)

    raw = ""
    try:
        if provider == "anthropic":
            import anthropic
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            client = anthropic.Anthropic(api_key=api_key)
            m = model or DEFAULT_MODEL_ANTHROPIC
            msg = client.messages.create(
                model=m, max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text.strip()
        else:
            import ollama
            m = model or DEFAULT_MODEL_OLLAMA
            resp = ollama.generate(model=m, prompt=prompt)
            raw = resp.response if hasattr(resp, "response") else resp.get("response", "")
    except Exception as exc:
        log.error("Failed to generate categories: %s", exc)
        return []

    raw = _strip_thinking(raw)
    start, end = raw.find("["), raw.rfind("]") + 1
    if start < 0 or end <= start:
        log.error("No JSON array found in category response.")
        return []
    try:
        pairs = json.loads(raw[start:end])
    except json.JSONDecodeError as exc:
        log.error("JSON parse error in category response: %s", exc)
        return []

    result = []
    for p in pairs:
        if isinstance(p, dict) and p.get("category") and p.get("letter"):
            letter = str(p["letter"]).strip().upper()
            # Accept single alpha letter or "*" for any-letter questions
            if letter == "*" or (len(letter) == 1 and letter.isalpha()):
                result.append({"category": str(p["category"]).strip(), "letter": letter})
    return result


def auto_generate(
    provider: str,
    per_difficulty: int = 5,
    answer_count: int = 12,
    model: str | None = None,
    categories_template: str | None = None,
    answers_template: str | None = None,
) -> None:
    """Generate questions for all three difficulties without any manual input."""
    difficulties = ["easy", "medium", "hard"]
    total = 0

    for difficulty in difficulties:
        print(f"\n{'='*50}")
        print(f"  Generating {per_difficulty} questions — {difficulty.upper()}")
        print(f"{'='*50}")

        log.info("Asking AI to suggest %d category+letter pairs for %s...", per_difficulty, difficulty)
        entries = _generate_categories(
            provider, difficulty, per_difficulty, model,
            categories_template=categories_template,
        )

        if not entries:
            log.warning("No categories returned for %s — skipping.", difficulty)
            continue

        log.info("Got %d pairs: %s", len(entries),
                 ", ".join(f"{e['category']}/{e['letter']}" for e in entries))

        added = generate_and_save(
            provider, entries, difficulty,
            answer_count=answer_count, model=model,
            answers_template=answers_template,
        )
        total += added

    print(f"\n✅  Auto-generation complete — {total} question(s) added across all difficulties.")


# ── Core generation function ───────────────────────────────────────────────────

def generate_and_save(
    provider: str,
    entries: list[dict],  # list of {category, letter, hint?, time_limit?}
    difficulty: str,
    answer_count: int = 12,
    model: str | None = None,
    answers_template: str | None = None,
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

        kw: dict = {}
        if model:
            kw["model"] = model
        if answers_template:
            kw["answers_template"] = answers_template

        if provider == "anthropic":
            answers = generate_with_anthropic(category, letter, difficulty, count=answer_count, **kw)
        else:
            answers = generate_with_ollama(category, letter, difficulty, count=answer_count, **kw)

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

def run_interactive(provider: str, answers_template: str | None = None) -> None:
    print("\n🐱⚡  SigmoCatClash — Question Generator")
    print("=" * 42)
    difficulty = input("Difficulty (easy / medium / hard) [medium]: ").strip().lower() or "medium"
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    print(f"Difficulty set to: {difficulty}\n")

    entries: list[dict] = []
    print("Enter categories (blank line to finish).")
    print("Format:  Category Name, Letter  (e.g.  Things in a kitchen, B)")
    print("         Use * as letter for 'any letter' questions\n")

    while True:
        line = input("> ").strip()
        if not line:
            break
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            print("  ⚠  Format must be: 'Category, Letter'")
            continue
        letter = parts[1].strip().upper()
        if letter != "*" and (not parts[1].isalpha() or len(parts[1].strip()) != 1):
            print("  ⚠  Letter must be a single letter or * for any")
            continue
        entries.append({"category": parts[0], "letter": letter})
        print(f"  Added: {parts[0]} → {letter}")

    if not entries:
        print("Nothing to generate.")
        return

    generate_and_save(provider, entries, difficulty, answers_template=answers_template)


def run_bulk(
    provider: str,
    difficulty: str,
    raw_entries: list[str],
    answers_template: str | None = None,
) -> None:
    entries: list[dict] = []
    for raw in raw_entries:
        if ":" not in raw:
            log.warning("Skipping '%s' — expected format 'Category:Letter'", raw)
            continue
        cat, letter = raw.rsplit(":", 1)
        letter = letter.strip().upper()
        if letter != "*" and (not letter.isalpha() or len(letter) != 1):
            log.warning("Skipping '%s' — invalid letter '%s'", raw, letter)
            continue
        entries.append({"category": cat.strip(), "letter": letter})

    if not entries:
        log.error("No valid entries to process.")
        sys.exit(1)

    generate_and_save(provider, entries, difficulty, answers_template=answers_template)


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
    parser.add_argument("--auto", action="store_true", help="Auto-generate categories and answers for all difficulties")
    parser.add_argument(
        "--bulk", nargs="+", metavar="CATEGORY:LETTER",
        help="Generate questions for listed categories (format: 'Kitchen things:B' or 'Chief Ministers:*')",
    )
    parser.add_argument("--category", help="Single category name")
    parser.add_argument("--letter",   help="Single letter constraint (use * for any letter)")
    parser.add_argument("--hint",     default="", help="Optional hint for single-category mode")
    parser.add_argument(
        "--count", type=int, default=12,
        help="Number of answers to generate per question (default: 12)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Override model name (e.g. qwen3:4b, llama3.2, claude-haiku-4-5-20251001)",
    )
    parser.add_argument(
        "--categories-prompt",
        default=None,
        metavar="FILE",
        help=(
            "Prompt template file for generating category+letter suggestions "
            "(default: prompts/categories.txt). "
            "Must contain {count} and {difficulty} placeholders. "
            "Example: prompts/india_kerala_world.txt"
        ),
    )
    parser.add_argument(
        "--answers-prompt",
        default=None,
        metavar="FILE",
        help=(
            "Prompt template file for generating answers for a given category+letter "
            "(default: prompts/answers.txt). "
            "Must contain {category}, {letter}, {difficulty}, {count} placeholders."
        ),
    )

    args = parser.parse_args()

    # Load prompt templates
    cat_prompt_path = _resolve_prompt_path(args.categories_prompt, DEFAULT_CATEGORIES_PROMPT_FILE)
    ans_prompt_path = _resolve_prompt_path(args.answers_prompt, DEFAULT_ANSWERS_PROMPT_FILE)

    categories_template = load_prompt_template(cat_prompt_path)
    answers_template    = load_prompt_template(ans_prompt_path)

    log.info("Categories prompt: %s", cat_prompt_path)
    log.info("Answers prompt:    %s", ans_prompt_path)

    if args.interactive:
        run_interactive(args.provider, answers_template=answers_template)

    elif args.auto:
        auto_generate(
            args.provider,
            per_difficulty=5,
            answer_count=args.count,
            model=args.model,
            categories_template=categories_template,
            answers_template=answers_template,
        )

    elif args.bulk:
        run_bulk(args.provider, args.difficulty, args.bulk, answers_template=answers_template)

    elif args.category and args.letter:
        letter = args.letter.strip().upper()
        if letter != "*" and (not letter.isalpha() or len(letter) != 1):
            log.error("--letter must be a single alphabet character or *")
            sys.exit(1)
        entry = {
            "category": args.category,
            "letter":   letter,
            "hint":     args.hint,
        }
        generate_and_save(
            args.provider, [entry], args.difficulty,
            answer_count=args.count, model=args.model,
            answers_template=answers_template,
        )

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
