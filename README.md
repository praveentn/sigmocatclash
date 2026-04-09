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

## Tech Stack

- [py-cord](https://github.com/Pycord-Development/pycord) — Discord API wrapper
- [aiohttp](https://docs.aiohttp.org/) — async health-check web server
- [python-dotenv](https://pypi.org/project/python-dotenv/) — environment config
