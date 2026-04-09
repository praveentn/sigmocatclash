"""
SigmoCatClash — Discord bot entry point.

Environment variables (see .env.example):
  DISCORD_TOKEN      — required
  DISCORD_GUILD_ID   — optional; restricts slash commands to one guild for
                       instant dev registration instead of ~1 hr global rollout
  PORT               — optional; health-check HTTP server port (default 8080)
                       Railway and similar hosts use this to verify the process
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import discord
from aiohttp import web
from dotenv import load_dotenv

# ── Bootstrap ──────────────────────────────────────────────────────────────────
load_dotenv()

# Ensure logs/ directory exists
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
TOKEN    = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = os.getenv("DISCORD_GUILD_ID", "").strip()
PORT     = int(os.getenv("PORT", "8080"))


debug_guilds = [int(GUILD_ID)] if GUILD_ID.isdigit() else None
if debug_guilds:
    log.info("Dev mode: slash commands registered to guild %s (instant)", GUILD_ID)
else:
    log.info("Production mode: slash commands registered globally (~1 hr propagation)")

# ── Health-check web server (for Railway / cloud hosts) ────────────────────────

async def _health_handler(request: web.Request) -> web.Response:
    # `bot` is assigned inside __main__ after event-loop setup; guard for startup
    _bot = globals().get("bot")
    ready = _bot is not None and _bot.is_ready()
    payload = {
        "status": "ok",
        "bot": str(_bot.user) if ready else "starting",
        "guilds": len(_bot.guilds) if ready else 0,
        "latency_ms": round(_bot.latency * 1000, 1) if ready else None,
    }
    return web.Response(
        text=json.dumps(payload),
        content_type="application/json",
    )


async def _start_health_server() -> None:
    app = web.Application()
    app.router.add_get("/", _health_handler)
    app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health-check server listening on :%d", PORT)


# ── Bot ────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True   # Privileged intent — MUST be enabled in Dev Portal


class SigmoCatBot(discord.Bot):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    async def on_ready(self) -> None:
        # Start health server once the event loop is fully running
        asyncio.create_task(_start_health_server())

        guild_count = len(self.guilds)
        log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        log.info("  SigmoCatClash is ONLINE! 🐱⚡")
        log.info("  Logged in as : %s  (ID: %s)", self.user, self.user.id)
        log.info("  Guilds       : %d", guild_count)
        log.info("  Latency      : %.1f ms", self.latency * 1000)
        log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        await self.change_presence(
            activity=discord.Game(name="/play to start Category Clash!")
        )

    async def on_application_command_error(
        self,
        ctx: discord.ApplicationContext,
        error: discord.DiscordException,
    ) -> None:
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.respond(
                f"⏳ Command on cooldown — try again in {error.retry_after:.1f}s.",
                ephemeral=True,
            )
        elif isinstance(error, commands.MissingPermissions):
            await ctx.respond("🚫 You don't have permission to use that command.", ephemeral=True)
        else:
            log.error("Unhandled slash command error: %s", error, exc_info=True)
            try:
                await ctx.respond(
                    "❌ Something went wrong. Please try again.", ephemeral=True
                )
            except discord.HTTPException:
                pass


# Pycord needs `commands` imported for the error handler isinstance check above
from discord.ext import commands  # noqa: E402 (after bot class so log is ready)

# ── Run ────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Python 3.10+ no longer auto-creates an event loop on get_event_loop().
    # py-cord accesses the loop during Bot.__init__, so we set one first.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    bot = SigmoCatBot(intents=intents, debug_guilds=debug_guilds)

    # Load cog before bot.run() — pycord registers commands at this point
    bot.load_extension("cogs.game")
    log.info("Game cog loaded.")


