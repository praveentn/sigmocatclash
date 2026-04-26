"""
SigmoCatClash — Discord bot entry point.

Environment variables (see .env.example):
  DISCORD_TOKEN      — required
  DISCORD_GUILD_ID   — optional; restricts slash commands to one guild for
                       instant dev registration instead of ~1 hr global rollout
  PORT               — optional; health-check HTTP server port (default 8080)
"""

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

import discord
from aiohttp import web
from dotenv import load_dotenv

import db

load_dotenv()

# ── Bootstrap ──────────────────────────────────────────────────────────────────
Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("sigmocatclash")

# ── Config ─────────────────────────────────────────────────────────────────────
TOKEN        = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID     = os.getenv("DISCORD_GUILD_ID", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PORT         = int(os.getenv("PORT", "8080"))
START_TIME   = time.time()

_debug_guilds = [int(GUILD_ID)] if GUILD_ID.isdigit() else None

# Python 3.10+ no longer auto-creates an event loop at module scope.
# discord.Bot.__init__ calls asyncio.get_event_loop(), so set one first.
asyncio.set_event_loop(asyncio.new_event_loop())

# ── Bot ────────────────────────────────────────────────────────────────────────
from discord.ext import commands  # noqa: E402

intents = discord.Intents.default()
intents.message_content = True   # Privileged — enable in Discord Dev Portal → Bot

bot = discord.Bot(intents=intents, debug_guilds=_debug_guilds)

# Load game cog — wrapped so a bad import doesn't silently wipe all slash commands
_COGS = ["cogs.game", "cogs.reminders"]
for _cog in _COGS:
    try:
        bot.load_extension(_cog)
        log.info("Loaded cog: %s", _cog)
    except Exception as _exc:
        # Log and exit: if the cog fails, sync_commands() would push an empty
        # command list and every slash command would vanish from Discord.
        log.critical("FAILED to load cog %s: %s — aborting startup.", _cog, _exc, exc_info=True)
        sys.exit(1)

# ── Web server (health check + status page) ────────────────────────────────────

_STATUS_HTML = """\
<!DOCTYPE html><html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>SigmoCatClash Status</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       background:#1a1a2e;color:#e0e0e0;padding:32px 16px}}
  .wrap{{max-width:640px;margin:0 auto}}
  h1{{color:#7289da;font-size:1.8rem;margin-bottom:24px}}
  h2{{color:#99aab5;font-size:.85rem;text-transform:uppercase;
      letter-spacing:.08em;margin-bottom:10px}}
  .card{{background:#16213e;border-radius:10px;padding:18px 22px;margin-bottom:16px}}
  ul{{list-style:none;line-height:2.2}}
  .ok{{color:#43b581}} .warn{{color:#faa61a}}
  footer{{margin-top:24px;color:#555;font-size:.8rem;text-align:center}}
</style></head>
<body><div class="wrap">
  <h1>🐱⚡ SigmoCatClash</h1>
  {content}
  <footer>Auto-refreshes every 10 s &nbsp;·&nbsp; <a href="/health" style="color:#7289da">/health JSON</a></footer>
</div></body></html>"""


async def _status_page(request: web.Request) -> web.Response:
    up  = int(time.time() - START_TIME)
    uptime = f"{up // 3600}h {(up % 3600) // 60}m {up % 60}s"
    online = bool(bot.user)

    rows = [
        f'<li>{"✅" if online else "⏳"} Discord: '
        f'{"<span class=ok>connected as <b>" + str(bot.user) + "</b></span>" if online else "<span class=warn>connecting…</span>"}</li>',
        f"<li>✅ HTTP server: <span class=ok>port {PORT}</span></li>",
        f"<li>⏱ Uptime: {uptime}</li>",
    ]
    content = f'<div class=card><h2>Status</h2><ul>{"".join(rows)}</ul></div>'

    if online:
        content += (
            f'<div class=card><h2>Bot Info</h2><ul>'
            f'<li>🤖 {bot.user}</li>'
            f'<li>🏠 Guilds: {len(bot.guilds)}</li>'
            f'<li>📡 Latency: {round(bot.latency * 1000)} ms</li>'
            f'</ul></div>'
        )

    return web.Response(
        text=_STATUS_HTML.format(content=content),
        content_type="text/html",
    )


async def _health_json(request: web.Request) -> web.Response:
    return web.json_response({
        "status":     "online",
        "discord":    str(bot.user) if bot.user else None,
        "guilds":     len(bot.guilds),
        "latency_ms": round(bot.latency * 1000, 2) if bot.user else None,
        "uptime_s":   int(time.time() - START_TIME),
        "port":       PORT,
    })


async def _run_web_server() -> None:
    app = web.Application()
    app.router.add_get("/",       _status_page)
    app.router.add_get("/health", _health_json)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Status page  →  http://localhost:%d/", PORT)
    log.info("Health JSON  →  http://localhost:%d/health", PORT)


# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready() -> None:
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    log.info("  SigmoCatClash is ONLINE! 🐱⚡")
    log.info("  Logged in as : %s  (ID: %s)", bot.user, bot.user.id)
    log.info("  Guilds       : %d", len(bot.guilds))
    log.info("  Latency      : %.1f ms", bot.latency * 1000)
    if _debug_guilds:
        log.info("  Mode         : dev — guild %s (instant commands)", GUILD_ID)
    else:
        log.info("  Mode         : global commands (~1 hr propagation)")
    log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # ── Slash command sync ────────────────────────────────────────────────────
    # py-cord auto-syncs on connect, but calling it here gives us:
    #   • a guaranteed second attempt after the bot is fully ready
    #   • a visible log entry that confirms *which* commands were pushed
    #   • an error log if the sync itself fails (e.g. Discord rate-limit on cold start)
    try:
        await bot.sync_commands()
        synced = [c.name for c in bot.pending_application_commands]
        log.info(
            "Slash commands synced (%s): %s",
            f"guild {GUILD_ID}" if _debug_guilds else "global",
            synced if synced else "⚠ EMPTY — check cog load above!",
        )
    except Exception as exc:
        log.error("sync_commands() FAILED: %s", exc, exc_info=True)

    # ── Stale guild-command cleanup ───────────────────────────────────────────
    # When switching from guild-scoped mode (DISCORD_GUILD_ID was set) to global
    # mode, Discord keeps the old guild-specific registrations alive. They show
    # up as duplicate / ghost commands in the server's / menu until cleared.
    # This block checks every guild the bot is in and wipes any guild-scoped
    # registrations so only the global commands remain.  It is a no-op on all
    # subsequent restarts once the stale entries are gone.
    if not _debug_guilds:
        cleaned_guilds = 0
        for guild in bot.guilds:
            try:
                existing = await bot.http.get_guild_commands(bot.user.id, guild.id)
                if existing:
                    await bot.http.bulk_upsert_guild_commands(bot.user.id, guild.id, [])
                    log.info(
                        "  Cleared %d stale guild-scoped command(s) from guild %d (%s).",
                        len(existing), guild.id, guild.name,
                    )
                    cleaned_guilds += 1
            except Exception as exc:
                log.warning(
                    "  Could not clear guild commands for %d (%s): %s",
                    guild.id, guild.name, exc,
                )
        if cleaned_guilds:
            log.info("Guild-command cleanup done — cleared %d guild(s).", cleaned_guilds)
        else:
            log.info("No stale guild commands found — nothing to clean up.")

    await bot.change_presence(activity=discord.Game(name="/play to start Category Clash!"))


@bot.event
async def on_application_command_error(ctx: discord.ApplicationContext, error) -> None:
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.respond(f"⏳ Cooldown — try again in {error.retry_after:.1f}s.", ephemeral=True)
    elif isinstance(error, commands.MissingPermissions):
        await ctx.respond("🚫 You don't have permission for that.", ephemeral=True)
    else:
        log.error("Slash command error: %s", error, exc_info=True)
        try:
            await ctx.respond("❌ Something went wrong. Please try again.", ephemeral=True)
        except discord.HTTPException:
            pass


# ── Entry point ────────────────────────────────────────────────────────────────

async def main() -> None:
    if not DATABASE_URL:
        log.critical("DATABASE_URL is not set — cannot start without a database.")
        sys.exit(1)
    await db.init(DATABASE_URL)
    await _run_web_server()   # start health server first so Railway sees the port
    try:
        await bot.start(TOKEN)
    finally:
        await db.close()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        log.info("Shutting down — goodbye!")
    finally:
        loop.close()
