# 🐱⚡ SigmoCatClash

**SigmoCatClash** is a fast-paced multiplayer Discord trivia bot. Race friends in **Category Clash** — name things from a category that start with a given letter before the timer runs out. First to claim an answer scores the point. No repeats, no second chances. Track scores across rounds and top the leaderboard!

---

## Commands

| Command | Description |
|---|---|
| `/play [rounds] [difficulty]` | Start a Category Clash game (1–10 rounds, easy/medium/hard/all) |
| `/scores` | Show the live leaderboard mid-game |
| `/stop` | End the current game (host or moderators only) |
| `/rules` | Show how to play |

## How it works

1. The bot announces a **category** (e.g. *Things in a kitchen*) and a **letter** (e.g. **B**).
2. Players race to type matching words — `Bowl`, `Bread, Butter`, etc.
3. ✅ = answer claimed!  🔁 = already taken by someone else.
4. Each unique valid answer is **+1 point**.
5. After all rounds the final leaderboard is revealed.

## Setup

See [SETUP.md](SETUP.md) for full installation and Railway deployment instructions.

## Question Generator

Use `scripts/generate_questions.py` (Ollama or Anthropic API) to add more questions to `data/questions/`.

```bash
python scripts/generate_questions.py --interactive
python scripts/generate_questions.py --provider anthropic --category "Famous scientists" --letter D --difficulty medium
```

Commands to run
Mode 1 — Bulk (fastest, everything in one API call per batch):
```
# Add 50 more to easy + medium (currently at 150/310), skip hard (already 300):
python scripts/bulk_generate.py --target 200

# India/Kerala/World themed questions into medium:
python scripts/bulk_generate.py --target 370 --diff medium --prompt-file prompts/india_kerala_world.txt
```

Mode 2 — Auto with saved categories (what you asked for):

```
# Step 1: generate 50 new categories per difficulty, then 10 answers each
# Saves categories to data/categories.json automatically
python scripts/bulk_generate.py --auto --categories-per-diff 50 --answers-per-cat 10

# Later: regenerate/extend answers using those same saved categories
python scripts/bulk_generate.py --auto --use-saved --answers-per-cat 12

# Just save categories now, generate questions later
python scripts/bulk_generate.py --auto --save-categories-only --categories-per-diff 50
# ... then when ready:
python scripts/bulk_generate.py --auto --use-saved --answers-per-cat 10
```

The saved categories file is data/categories.json — it accumulates over time and deduplicates automatically. Each run with --auto (without --use-saved) generates new categories and appends them to the file.

Mode 3

```
python scripts/bulk_generate.py --auto --categories-prompt prompts/categories_india_kerala_world.txt --categories-per-diff 50 --answers-per-cat 10

```


## Tech Stack

- [py-cord](https://github.com/Pycord-Development/pycord) — Discord API wrapper
- [aiohttp](https://docs.aiohttp.org/) — async health-check web server
- [python-dotenv](https://pypi.org/project/python-dotenv/) — environment config
