# 🐱⚡ SigmoCatClash

**SigmoCatClash** is a fast-paced multiplayer Discord trivia bot. Race friends in **Category Clash** — name things from a category that start with a given letter before the timer runs out. First to claim an answer scores the point. No repeats, no second chances. Track scores, climb ranks, and build daily streaks!

---

## Commands

| Command | Description |
|---|---|
| `/play [rounds] [difficulty]` | Start a Category Clash game (1–10 rounds, easy/medium/hard/all/daily) |
| `/scores` | Show the live leaderboard mid-game |
| `/stop` | End the current game (host or moderators only) |
| `/rules` | Show how to play |
| `/leaderboard [view]` | All-time top players — by `scores` (default), `streaks`, or `wins` |
| `/mystats [user]` | Your personal profile: rank, streak, achievements, full stats |

---

## How it works

1. The bot announces a **category** (e.g. *Things in a kitchen*) and a **letter** (e.g. **B**).
2. Players race to type matching words — `Bowl`, `Bread, Butter`, etc.
3. ✅ = answer claimed!  🔁 = already taken by someone else.
4. Each unique valid answer is **+1 point**. Bonuses stack:
   - ⚡ **Speed bonus** — +1 pt to the very first scorer each round
   - 🔥 **Streak bonus** — +1 pt for your 3rd+ consecutive valid answer in a round
5. After all rounds the final leaderboard is revealed.

---

## Rank System

Earn points across games to climb through rank tiers:

| Rank | Min Points |
|---|---|
| 🪨 Rookie | 0 |
| 🥉 Player | 50 |
| 🥈 Pro | 150 |
| 🥇 Elite | 350 |
| 💎 Master | 700 |
| 👑 Legend | 1200 |

Your current rank is shown in `/mystats` and on the `/leaderboard`.

---

## Daily Streaks

Play at least one game **every day** to build a daily streak. Your streak resets if you miss a day.

- Streaks are tracked per player and shown in `/leaderboard streaks`
- Long streaks unlock achievements and show as 🔥 in the post-game highlights

---

## Achievements

Unlock badges by hitting milestones. Shown in `/mystats`.

| Category | Milestones |
|---|---|
| 🎮 Games played | 1, 5, 10, 25, 50, 100 |
| 🥇 Wins | 1, 5, 10, 25 |
| 📈 Total score | 50, 150, 300, 500, 1000 |
| 🔝 Best single game | 20, 30, 40+ pts |
| 🔥 Daily streak | 3, 7, 14, 30 days in a row |

New achievements are announced in the **post-game Highlights** section the moment you unlock them.

---

## Addiction Hooks

The game is designed to keep you coming back:

- **Post-game Highlights** — streak fire 🔥, rank-up announcements ⬆️, achievement unlocks 🏅
- **Close-race tension** — "RAZOR CLOSE — only 1 pt in it!" shown in final results
- **Rank progress nudge** — "just 12 pts from the next rank!" after close milestones
- **Daily streak pressure** — your streak resets if you miss a day; shown on every leaderboard
- **Three leaderboard views** — compete on score, wins, *and* daily consistency

---

## Setup

See [SETUP.md](SETUP.md) for full installation and Railway deployment instructions.

---

## Question Generator

Use `scripts/generate_questions.py` (Ollama or Anthropic API) to add more questions to `data/questions/`.

```bash
python scripts/generate_questions.py --interactive
python scripts/generate_questions.py --provider anthropic --category "Famous scientists" --letter D --difficulty medium
```

### Bulk generation

Mode 1 — Bulk (fastest, everything in one API call per batch):
```bash
# Add 50 more to easy + medium (currently at 150/310), skip hard (already 300):
python scripts/bulk_generate.py --target 200

# India/Kerala/World themed questions into medium:
python scripts/bulk_generate.py --target 370 --diff medium --prompt-file prompts/india_kerala_world.txt
```

Mode 2 — Auto with saved categories:
```bash
# Step 1: generate 50 new categories per difficulty, then 10 answers each
python scripts/bulk_generate.py --auto --categories-per-diff 50 --answers-per-cat 10

# Later: regenerate/extend answers using those same saved categories
python scripts/bulk_generate.py --auto --use-saved --answers-per-cat 12

# Just save categories now, generate questions later
python scripts/bulk_generate.py --auto --save-categories-only --categories-per-diff 50
python scripts/bulk_generate.py --auto --use-saved --answers-per-cat 10
```

Mode 3 — Custom category prompt:
```bash
python scripts/bulk_generate.py --auto --categories-prompt prompts/categories_india_kerala_world.txt --categories-per-diff 50 --answers-per-cat 10
```

The saved categories file is `data/categories.json` — it accumulates over time and deduplicates automatically.

---

## Tech Stack

- [py-cord](https://github.com/Pycord-Development/pycord) — Discord API wrapper
- [aiohttp](https://docs.aiohttp.org/) — async health-check web server
- [python-dotenv](https://pypi.org/project/python-dotenv/) — environment config
