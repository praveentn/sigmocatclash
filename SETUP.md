# SigmoCatClash — Setup Guide

## Prerequisites

- Python **3.10** or higher
- A Discord account with a server where you have **Manage Server** permission
- (Optional) [Ollama](https://ollama.ai) or an **Anthropic API key** for generating extra questions

---

## 1. Create a Discord Application & Bot

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Name it (e.g. `SigmoCatClash`) and click **Create**.
3. Navigate to **Bot** → click **Add Bot** → confirm.
4. Under **Token** click **Reset Token**, copy the token — you'll need it shortly.
5. Scroll down to **Privileged Gateway Intents** and enable:
   - ✅ **Message Content Intent** *(required — the bot reads player answers)*
6. Click **Save Changes**.

## 2. Invite the Bot to Your Server

1. Go to **OAuth2 → URL Generator**.
2. Scopes: ✅ `bot`  ✅ `applications.commands`
3. Bot Permissions: ✅ `Send Messages`  ✅ `Embed Links`  ✅ `Add Reactions`  ✅ `Read Message History`
4. Copy the generated URL, open it in a browser, select your server, and click **Authorize**.

---

## 3. Local Setup (Windows)

```cmd
cd path\to\sigmocatclash

copy .env.example .env
```

Open `.env` in any text editor and paste your bot token:

```
DISCORD_TOKEN=your_actual_token_here
DISCORD_GUILD_ID=your_guild_id_here   # optional — speeds up slash command registration during dev
```

> **Get your Guild ID:** Enable Developer Mode in Discord Settings → User Settings → Advanced.
> Right-click your server icon → **Copy Server ID**.

Then simply run:

```cmd
start.bat
```

`start.bat` will automatically create a `.venv`, install dependencies, validate your token, and start the bot.

---

## 4. Local Setup (macOS / Linux)

```bash
cd path/to/sigmocatclash
cp .env.example .env
# edit .env and paste DISCORD_TOKEN (and optionally DISCORD_GUILD_ID)

chmod +x start.sh
./start.sh
```

---

## 5. Adding More Questions

The bot ships with questions in `data/questions/easy.csv`, `medium.csv`, and `hard.csv`.

Use the question generator to add your own:

```bash
# Install generator deps (once)
pip install -r scripts/requirements_gen.txt

# Interactive mode (walks you through it)
python scripts/generate_questions.py --interactive

# Single category via CLI
python scripts/generate_questions.py \
  --provider ollama \
  --category "Things you find in a kitchen" \
  --letter B \
  --difficulty easy

# Multiple categories at once
python scripts/generate_questions.py \
  --provider anthropic \
  --difficulty medium \
  --bulk "Famous scientists:N" "Board games:S" "Movies:T"
```

### Ollama setup (local, free)

```bash
# Install Ollama from https://ollama.ai, then pull the model:
ollama pull llama3.2
# The generator defaults to this model; the Ollama server must be running.
```

### Anthropic API setup

Set `ANTHROPIC_API_KEY` in your `.env` or shell, then use `--provider anthropic`.

---

## 6. Deploying to Railway

> Railway runs on **Linux** — use `start.sh` conventions, not `start.bat`.

### Recommended Railway settings

| Setting | Value |
|---|---|
| **Build command** | `bash start.sh --setup` |
| **Start command** | `python bot.py` |

### Environment variables to set in Railway dashboard

| Variable | Value |
|---|---|
| `DISCORD_TOKEN` | Your bot token |
| `DISCORD_GUILD_ID` | *(leave blank for global slash commands)* |
| `PORT` | `8080` *(Railway injects this automatically)* |

> **Note:** With `DISCORD_GUILD_ID` blank, slash commands register globally and may take up to **1 hour** to appear in Discord.  
> Set `DISCORD_GUILD_ID` during development for **instant** command registration.

### Health check

The bot exposes a JSON health endpoint at `GET /health` (and `GET /`) on the configured `PORT`.  
Railway will use this to confirm the process is alive.

---

## 7. Slash Command Registration Notes

- **Guild commands** (`DISCORD_GUILD_ID` set): appear **instantly** in that server. Best for dev/testing.
- **Global commands** (`DISCORD_GUILD_ID` empty): propagate to all servers in **~1 hour**. Use for production.

---

## Commands Quick Reference

| Command | Description |
|---|---|
| `/play [rounds] [difficulty]` | Start a game (default: 5 rounds, all difficulties) |
| `/scores` | Live leaderboard mid-game |
| `/stop` | End the game (host or mods only) |
| `/rules` | Show how to play |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Slash commands don't appear | Wait up to 1 hr (global), or set `DISCORD_GUILD_ID` for instant dev registration |
| `LoginFailure` on startup | Double-check `DISCORD_TOKEN` in `.env` — no extra spaces |
| Bot doesn't see player answers | Enable **Message Content Intent** in the Developer Portal → Bot settings |
| Bot missing permissions | Re-invite with correct scopes (Send Messages, Embed Links, Add Reactions) |
| No questions available | Check `data/questions/*.csv` exist and are not empty |
