"""
Daily reminder cog — sends one reminder per guild per day at 08:00 AM local time.

Admin commands (all under /remind, require Manage Server):
  /remind channel #channel           — set the target channel
  /remind timezone America/New_York  — set IANA timezone; confirms with current local time
  /remind status                     — show current configuration
  /remind test                       — fire a test reminder immediately
  /remind off                        — disable reminders

The reminder embed carries four persistent action buttons:
  ▶️ Play Now · 🏆 Leaderboard · 📊 My Stats · 📜 Rules

"Persistent" means timeout=None + explicit custom_id on every button, and the
view is registered with bot.add_view() on startup so interactions still route
correctly after a bot restart.

The background loop fires every minute and checks whether any configured guild
has crossed the 08:00 AM threshold (up to 10:00 AM) without a reminder today.
The 2-hour window means a bot restart after 08:00 still sends the reminder
rather than skipping the whole day.
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import option
from discord.ext import commands, tasks

from game.leaderboard import (
    get_guild_players,
    get_overall_leaderboard,
    get_player_stats,
    get_rank,
)
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


# ── Persistent reminder view ──────────────────────────────────────────────────

class ReminderView(discord.ui.View):
    """
    Action buttons attached to the daily reminder embed.

    timeout=None + per-button custom_id = persistent view.  Register once via
    bot.add_view(ReminderView()) on startup so old reminder messages keep
    working after a bot restart.  Each callback resolves guild context from
    interaction.guild_id at call time, so the view stores no mutable state.
    """

    def __init__(self) -> None:
        super().__init__(timeout=None)

    # ── internal helpers ──────────────────────────────────────────────────────

    async def _game_cog(self, interaction: discord.Interaction):
        """Return the SigmoCatClash cog, or respond with an error and return None."""
        cog = interaction.client.get_cog("SigmoCatClash")
        if cog is None:
            await interaction.response.send_message(
                "❌ Game is unavailable right now — try `/play` directly.", ephemeral=True,
            )
        return cog

    # ── buttons ───────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="▶️ Play Now",
        style=discord.ButtonStyle.success,
        custom_id="sigmo_reminder_play",
    )
    async def play_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        cog = await self._game_cog(interaction)
        if cog is None:
            return
        # launch_from_interaction owns the full interaction response lifecycle
        await cog.launch_from_interaction(interaction)

    @discord.ui.button(
        label="🏆 Leaderboard",
        style=discord.ButtonStyle.primary,
        custom_id="sigmo_reminder_lb",
    )
    async def leaderboard_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        try:
            entries = await get_overall_leaderboard(top_n=10, guild_id=guild_id)
        except Exception:
            log.exception("Leaderboard button DB error — guild %s", guild_id)
            await interaction.response.send_message(
                "❌ Could not fetch leaderboard right now.", ephemeral=True,
            )
            return

        if not entries:
            description = "No games played yet on this server — be the first! 🎮"
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines = [
                f"{medals[i] if i < 3 else f'**#{i + 1}**'}"
                f" <@{p['player_id']}> — {p.get('total_score', 0):,} pts"
                for i, p in enumerate(entries)
            ]
            description = "\n".join(lines)

        embed = discord.Embed(
            title="🏆 Server Leaderboard",
            description=description,
            color=0xFEE75C,
        )
        embed.set_footer(text="Use /leaderboard for streaks & wins views")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="📊 My Stats",
        style=discord.ButtonStyle.secondary,
        custom_id="sigmo_reminder_stats",
    )
    async def stats_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        guild_id = interaction.guild_id
        if not guild_id:
            await interaction.response.send_message("❌ Server only.", ephemeral=True)
            return

        try:
            stats = await get_player_stats(interaction.user.id, guild_id=guild_id)
        except Exception:
            log.exception("Stats button DB error — guild %s user %s", guild_id, interaction.user.id)
            await interaction.response.send_message(
                "❌ Could not fetch your stats right now.", ephemeral=True,
            )
            return

        if not stats:
            await interaction.response.send_message(
                "You haven't played any games on this server yet — hit **▶️ Play Now** to start!",
                ephemeral=True,
            )
            return

        total_score  = stats.get("total_score", 0)
        games_played = stats.get("games_played", 0)
        wins         = stats.get("wins", 0)
        best_score   = stats.get("best_score", 0)
        c_streak     = stats.get("current_streak", 0)
        rank_name, rank_emoji, next_thresh = get_rank(total_score)

        win_rate  = f"{wins / games_played * 100:.0f}%" if games_played > 0 else "—"
        rank_line = f"**{rank_emoji} {rank_name}**"
        if next_thresh > 0:
            rank_line += f"  *(+{next_thresh - total_score} pts to next rank)*"
        else:
            rank_line += "  *(max rank — Legend!)*"

        streak_str = (
            f"🔥 **{c_streak}-day streak**"
            if c_streak >= 1
            else "*No active streak — play today to start one!*"
        )

        embed = discord.Embed(
            title=f"📊 {interaction.user.display_name}'s Stats",
            color=0x9B59B6,
        )
        embed.add_field(name="🏅 Rank",         value=rank_line,                    inline=False)
        embed.add_field(name="⭐ Total Score",   value=f"**{total_score:,}** pts",   inline=True)
        embed.add_field(name="🎮 Games Played",  value=f"**{games_played}**",        inline=True)
        embed.add_field(name="🏆 Wins",          value=f"**{wins}** ({win_rate})",   inline=True)
        embed.add_field(name="🔝 Best Game",     value=f"**{best_score}** pts",      inline=True)
        embed.add_field(name="🔥 Streak",        value=streak_str,                   inline=True)
        embed.set_footer(text="Use /mystats for full details & achievements")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="📜 Rules",
        style=discord.ButtonStyle.secondary,
        custom_id="sigmo_reminder_rules",
    )
    async def rules_button(
        self, button: discord.ui.Button, interaction: discord.Interaction
    ) -> None:
        embed = discord.Embed(
            title="📖 SigmoCatClash — Quick Rules",
            description=(
                "**Each round:** A category + letter appear.\n"
                "Type matching words in chat — **first to claim an answer scores a point!**\n\n"
                "**Bonuses:**\n"
                "⚡ Speed bonus — first scorer each round\n"
                "🔥 Streak bonus — your 3rd+ consecutive valid answer in a round\n\n"
                "**Reactions:** ✅ scored · ⚡ speed · 🔥 streak · 🔁 already taken · ❌ invalid\n\n"
                "**Difficulty modes:** `easy` / `medium` / `hard` / `all` / `daily`\n\n"
                "**Commands:**\n"
                "`/play` — start a game  •  `/scores` — live scores\n"
                "`/leaderboard` — server rankings  •  `/mystats` — your profile\n"
                "`/stop` — end game (host / mods)  •  `/rules` — full rules"
            ),
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

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
        # Register here — not in __init__ — because View.__init__(timeout=None)
        # requires a running event loop, which doesn't exist at cog load time.
        self.bot.add_view(ReminderView())

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
        # All players who have ever played at least one game on this server.
        # get_guild_players does SELECT * FROM players WHERE guild_id=$1 — no
        # date filter — so this is the full registered roster for the guild.
        all_players = await get_guild_players(guild_id=guild_id)

        # Players whose active streak will break if they skip today
        at_risk = sorted(
            [
                p for p in all_players
                if p.get("current_streak", 0) > 0 and p.get("last_played") == yesterday_str
            ],
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

        # Attach persistent action buttons to the embed message
        await channel.send(embed=embed, view=ReminderView())

        # Paginated @mentions — every player who has ever played on this server
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
        """Chunk @mentions so each message stays under Discord's 2000-char limit."""
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for p in players:
            mention = f"<@{p['player_id']}>"
            # +1 for the space separator between mentions
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
