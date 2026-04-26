"""
Daily reminder cog — sends one reminder per guild per day at 08:00 AM local time
to every player who hasn't played today yet.

Admin commands (all under /remind, require Manage Server):
  /remind channel #channel           — set the target channel
  /remind timezone America/New_York  — set IANA timezone; confirms with current local time
  /remind status                     — show current configuration
  /remind test                       — fire a test reminder immediately
  /remind off                        — disable reminders

The background loop fires every minute and checks whether any configured guild
has crossed the 08:00 AM threshold (up to 10:00 AM) without a reminder today.
The 2-hour window means a bot restart after 08:00 still sends the reminder rather
than skipping the whole day.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import option
from discord.ext import commands, tasks

from game.leaderboard import get_overall_leaderboard, get_guild_players
from utils.categoryhistory import get_today_history
from utils.guild_settings import (
    get_all_guild_settings,
    get_guild_settings,
    remove_guild_settings,
    save_guild_settings,
)

log = logging.getLogger("sigmocatclash.reminders")

_REMINDER_HOUR = 8    # send at 08:00 AM
_REMINDER_CUTOFF = 10  # but only up to 10:00 AM (catch-up window for restarts)

_DAY_VIBES = {
    0: "Monday grind starts NOW — set the pace for the whole week! 💪",
    1: "Tuesday tuned-in — sharpen those category skills and climb the ranks! 🎯",
    2: "Wednesday warrior: you're halfway there — keep that streak alive! 🔥",
    3: "Thursday throwdown — who's making their move up the leaderboard today? 📈",
    4: "Friday energy: end the week on top and take the weekend glory! 🚀",
    5: "Saturday showdown — more free time means more games, more glory! 🎉",
    6: "Sunday strategy session — relax, play, and prep for next week's domination! 😎",
}


class RemindersCog(commands.Cog):
    remind = discord.SlashCommandGroup(
        "remind",
        "Configure daily 08:00 AM game reminders for this server",
        default_member_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._check_reminders.start()

    def cog_unload(self) -> None:
        self._check_reminders.cancel()

    # ── Background loop ───────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def _check_reminders(self) -> None:
        now_utc = datetime.now(ZoneInfo("UTC"))
        all_settings = await get_all_guild_settings()

        for guild_id_str, settings in all_settings.items():
            try:
                await self._maybe_remind(guild_id_str, settings, now_utc)
            except Exception as exc:
                log.error("Reminder check failed for guild %s: %s", guild_id_str, exc, exc_info=True)

    @_check_reminders.before_loop
    async def _before_check(self) -> None:
        await self.bot.wait_until_ready()

    async def _maybe_remind(self, guild_id_str: str, settings: dict, now_utc: datetime) -> None:
        channel_id = settings.get("reminder_channel_id")
        if not channel_id:
            return

        tz_name = settings.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            log.warning("Unknown timezone %r for guild %s — skipping", tz_name, guild_id_str)
            return

        local_now = now_utc.astimezone(tz)
        local_date_str = local_now.date().isoformat()

        # Only fire inside the [08:00, 10:00) window
        if not (_REMINDER_HOUR <= local_now.hour < _REMINDER_CUTOFF):
            return
        # Already sent today
        if settings.get("last_reminded_date") == local_date_str:
            return

        guild_id = int(guild_id_str)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        await self._send_daily_reminder(channel, guild, guild_id, local_now)

        settings["last_reminded_date"] = local_date_str
        await save_guild_settings(guild_id, settings)

    # ── Reminder builder ──────────────────────────────────────────────────────

    async def _send_daily_reminder(
        self,
        channel: discord.TextChannel,
        guild: discord.Guild,
        guild_id: int,
        local_now: datetime,
    ) -> None:
        today = local_now.date()
        yesterday_str = (today - timedelta(days=1)).isoformat()

        history_fact = get_today_history(local_now)
        top_players = await get_overall_leaderboard(top_n=3, guild_id=guild_id)
        all_players = await get_guild_players(guild_id=guild_id)

        at_risk = sorted(
            [p for p in all_players if p.get("current_streak", 0) > 0 and p.get("last_played") == yesterday_str],
            key=lambda p: p.get("current_streak", 0),
            reverse=True,
        )

        vibe = _DAY_VIBES.get(local_now.weekday(), "Time to play SigmoCatClash! 🎮")

        embed = discord.Embed(
            title=f"☀️ Daily Category Clash — {local_now.strftime('%A, %B %-d')}",
            description=f"📖 *{history_fact}*",
            color=0xFF8C00,
        )

        if top_players:
            medals = ["🥇", "🥈", "🥉"]
            lb_lines = [
                f"{medals[i]} <@{p['player_id']}> — {p.get('total_score', 0):,} pts"
                for i, p in enumerate(top_players)
            ]
            embed.add_field(name="🏆 Server Champions", value="\n".join(lb_lines), inline=False)

        if at_risk:
            risk_lines = [
                f"🔥 <@{p['player_id']}> — {p.get('current_streak', 0)}-day streak vanishes without a game today!"
                for p in at_risk[:5]
            ]
            embed.add_field(
                name="⚠️ Streaks at Risk — Don't Let the Fire Die!",
                value="\n".join(risk_lines),
                inline=False,
            )

        embed.add_field(name="💡 Today's Vibe", value=vibe, inline=False)
        embed.set_footer(text="Use /play to start a game  •  One game keeps your streak alive!")

        await channel.send(embed=embed)

        # Paginated @mentions — all registered players for this server
        if all_players:
            await self._send_paginated_mentions(channel, all_players)

        log.info(
            "Daily reminder sent — guild %d (%s)  channel %d  pinged %d player(s)",
            guild_id, guild.name, channel.id, len(all_players),
        )

    @staticmethod
    async def _send_paginated_mentions(
        channel: discord.TextChannel,
        players: list[dict],
    ) -> None:
        """Send @mention messages chunked to stay under Discord's 2000-char limit."""
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for p in players:
            mention = f"<@{p['player_id']}>"
            # +1 for the space separator
            if current_len + len(mention) + 1 > 1900:
                chunks.append(" ".join(current))
                current = [mention]
                current_len = len(mention)
            else:
                current.append(mention)
                current_len += len(mention) + 1

        if current:
            chunks.append(" ".join(current))

        for chunk in chunks:
            await channel.send(chunk)

    # ── Admin slash commands ──────────────────────────────────────────────────

    @remind.command(name="channel", description="Set the channel where daily reminders are posted")
    @option("channel", discord.TextChannel, description="Channel to post reminders in")
    async def remind_channel(
        self, ctx: discord.ApplicationContext, channel: discord.TextChannel
    ) -> None:
        if not ctx.guild_id:
            await ctx.respond("❌ This command can only be used inside a server.", ephemeral=True)
            return

        perms = channel.permissions_for(ctx.guild.me)
        if not (perms.send_messages and perms.embed_links):
            await ctx.respond(
                f"❌ I need **Send Messages** and **Embed Links** in {channel.mention}.",
                ephemeral=True,
            )
            return

        settings = await get_guild_settings(ctx.guild_id) or {}
        settings["reminder_channel_id"] = channel.id
        settings.setdefault("timezone", "UTC")
        settings.setdefault("last_reminded_date", "")
        await save_guild_settings(ctx.guild_id, settings)

        tz_name = settings["timezone"]
        tz_note = (
            f" (timezone: `{tz_name}`)"
            if tz_name != "UTC"
            else " — use `/remind timezone` to set your local time zone"
        )
        await ctx.respond(
            f"✅ Reminder channel set to {channel.mention}{tz_note}.\n"
            "Daily reminders fire at **08:00 AM** local time.",
            ephemeral=True,
        )
        log.info("Reminder channel set — guild %d  channel %d", ctx.guild_id, channel.id)

    @remind.command(name="timezone", description="Set the timezone for daily reminders (IANA name)")
    @option(
        "timezone",
        str,
        description="IANA timezone e.g. America/New_York · Asia/Kolkata · Europe/London",
    )
    async def remind_timezone(self, ctx: discord.ApplicationContext, timezone: str) -> None:
        if not ctx.guild_id:
            await ctx.respond("❌ This command can only be used inside a server.", ephemeral=True)
            return

        try:
            tz = ZoneInfo(timezone)
        except ZoneInfoNotFoundError:
            await ctx.respond(
                f"❌ Unknown timezone `{timezone}`.\n"
                "Use an IANA name like `America/New_York`, `Asia/Kolkata`, or `Europe/London`.\n"
                "Full list: <https://en.wikipedia.org/wiki/List_of_tz_database_time_zones>",
                ephemeral=True,
            )
            return

        local_now = datetime.now(tz)
        local_time_str = local_now.strftime("%I:%M %p, %A %B %-d")

        settings = await get_guild_settings(ctx.guild_id) or {}
        settings["timezone"] = timezone
        settings.setdefault("last_reminded_date", "")
        await save_guild_settings(ctx.guild_id, settings)

        await ctx.respond(
            f"✅ Timezone set to `{timezone}`.\n"
            f"🕐 Current local time: **{local_time_str}**\n"
            "Reminders fire at **08:00 AM** in this timezone.",
            ephemeral=True,
        )
        log.info("Reminder timezone set — guild %d  tz %s", ctx.guild_id, timezone)

    @remind.command(name="status", description="Show the current reminder configuration for this server")
    async def remind_status(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild_id:
            await ctx.respond("❌ This command can only be used inside a server.", ephemeral=True)
            return

        settings = await get_guild_settings(ctx.guild_id)

        if not settings or not settings.get("reminder_channel_id"):
            await ctx.respond(
                "ℹ️ **Reminders are not configured.**\n"
                "Use `/remind channel` to pick a channel, then `/remind timezone` to set local time.",
                ephemeral=True,
            )
            return

        channel_id = settings["reminder_channel_id"]
        channel = ctx.guild.get_channel(channel_id)
        channel_str = channel.mention if channel else f"⚠️ channel not found (`{channel_id}`)"

        tz_name = settings.get("timezone", "UTC")
        try:
            local_now = datetime.now(ZoneInfo(tz_name))
            time_str = local_now.strftime("%I:%M %p")
        except ZoneInfoNotFoundError:
            time_str = "unknown"

        last = settings.get("last_reminded_date") or "never"

        await ctx.respond(
            f"**📋 Reminder Configuration**\n"
            f"📣 Channel: {channel_str}\n"
            f"🌍 Timezone: `{tz_name}` (current: **{time_str}**)\n"
            f"⏰ Fires at: **08:00 AM** daily\n"
            f"📅 Last reminded: **{last}**",
            ephemeral=True,
        )

    @remind.command(name="test", description="Send a test reminder right now to see how it looks")
    async def remind_test(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild_id:
            await ctx.respond("❌ This command can only be used inside a server.", ephemeral=True)
            return

        settings = await get_guild_settings(ctx.guild_id)
        if not settings or not settings.get("reminder_channel_id"):
            await ctx.respond(
                "❌ No reminder channel configured. Use `/remind channel` first.",
                ephemeral=True,
            )
            return

        channel = ctx.guild.get_channel(settings["reminder_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            await ctx.respond("❌ Configured reminder channel not found.", ephemeral=True)
            return

        tz_name = settings.get("timezone", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            tz = ZoneInfo("UTC")

        await ctx.respond("📤 Sending test reminder now…", ephemeral=True)
        await self._send_daily_reminder(channel, ctx.guild, ctx.guild_id, datetime.now(tz))

    @remind.command(name="off", description="Disable daily reminders for this server")
    async def remind_off(self, ctx: discord.ApplicationContext) -> None:
        if not ctx.guild_id:
            await ctx.respond("❌ This command can only be used inside a server.", ephemeral=True)
            return

        if await get_guild_settings(ctx.guild_id) is None:
            await ctx.respond("ℹ️ No reminder is configured for this server.", ephemeral=True)
            return

        await remove_guild_settings(ctx.guild_id)
        await ctx.respond("✅ Daily reminders disabled.", ephemeral=True)
        log.info("Reminder disabled — guild %d", ctx.guild_id)


def setup(bot: discord.Bot) -> None:
    bot.add_cog(RemindersCog(bot))
