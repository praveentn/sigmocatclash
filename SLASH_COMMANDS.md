# Slash Command Registration — How It Works & Fixes Applied

## Background

Discord slash commands come in two scopes:

| Scope | Registration | Propagation |
|-------|-------------|-------------|
| **Guild** (via `DISCORD_GUILD_ID`) | Instant | Only in that server |
| **Global** (no guild ID) | Up to 1 hour | All servers the bot is in |

This bot uses **global commands** in production (Railway). Guild-scoped commands are only used locally for fast dev/test iteration.

---

## Problem: Commands Went Stale / Disappeared After Deploy

### Root cause

py-cord calls `sync_commands()` automatically during `on_connect`, before `on_ready`. This auto-sync can **silently fail** — e.g. due to a rate limit or a transient network error during Railway cold start — leaving the previous (or no) commands registered with Discord.

Additionally, when switching from guild commands → global commands, the old guild-scoped commands stay registered on Discord's side indefinitely and override or conflict with the new global ones.

---

## Fixes Applied

### 1. Explicit `sync_commands()` in `on_ready`

Added a guaranteed second sync attempt after the bot is fully connected. `on_ready` fires on every startup and every Railway redeploy.

```python
@bot.event
async def on_ready():
    from utils.database import init_db
    await init_db()
    print(f"✅  Discord  : {bot.user} (ID: {bot.user.id})")
    print(f"    Guilds  : {len(bot.guilds)}")
    print("    DB      : initialised")

    try:
        await bot.sync_commands()
        cmds = [c.name for c in bot.pending_application_commands]
        print(f"    Commands synced ({'guild' if DEBUG_GUILDS else 'global'} mode): {cmds}")
    except Exception as e:
        print(f"    ❌ sync_commands() FAILED: {e}")
```

The log line prints all registered command names, making it easy to verify in Railway logs that commands are actually being pushed to Discord.

### 2. Error handling around cog loading

Cog load failures were previously silent. If a cog fails to import, its commands are never registered with the bot and `sync_commands()` pushes an empty or partial list.

```python
for cog in COGS:
    try:
        bot.load_extension(cog)
        print(f"    Loaded cog: {cog}")
    except Exception as e:
        print(f"    ❌ Failed to load cog {cog}: {e}")
```

---

## Expected Startup Log (healthy)

```
Starting Container
    Status page  →  http://localhost:8080/
    Health JSON  →  http://localhost:8080/health
    Word list   : 5914 words loaded from words.txt
    Loaded cog: cogs.chain_cog
    Loaded cog: cogs.stats_cog
✅  Discord  : sigmoidian#3241 (ID: ...)
    Guilds  : 3
    DB      : initialised
    Commands synced (global mode): ['chain', 'addword', 'removeword', 'checkword', 'stats', 'leaderboard', 'topwords', 'dictionary']
```

If `sync_commands()` fails or the command list is empty, check for cog load errors above it.

---

## Checklist When Commands Stop Showing Up

1. **Check Railway logs** — confirm "Commands synced" printed with the full command list
2. **Restart Discord fully** — global commands are cached per client; Ctrl+R is not enough, fully quit and reopen
3. **Try Discord in a browser** (discord.com) — bypasses the desktop client cache entirely
4. **Verify bot invite URL** includes both scopes:
   ```
   scope=bot+applications.commands
   ```
   Without `applications.commands`, slash commands can never be registered regardless of what the code does.
5. **Wait up to 1 hour** — global command propagation is Discord's SLA, not the bot's

---

## Local Development

Set `DISCORD_GUILD_ID` in `.env` to get instant command registration in your test server:

```env
DISCORD_GUILD_ID=976816967161892976
```

Remove it (or leave it commented out) for global registration. **Never set this in Railway** — guild commands are for dev speed only.
