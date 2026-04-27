"""
SigmoCatClash — Category Clash game cog.

Flow per game:
  /play  →  5-second countdown  →  N rounds  →  final leaderboard

Each round:
  post question embed  →  accept text answers  →  half-time warning
  →  3-2-1 countdown  →  end_round  →  post results embed  →  5-sec gap

Security properties:
  • All leaderboard / stats operations are scoped to ctx.guild_id — no cross-server
    data leakage.
  • Guild-only guard on every command that writes or reads server-scoped data.
  • Winner identification uses player_id (not display name) to avoid name collisions.
  • Answer text is length-capped before fuzzy matching (session.py enforces ≤200 chars).
  • Command cooldowns prevent spam on read-heavy commands.
"""

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from game.achievements import ACHIEVEMENTS
from game.leaderboard import (
    get_overall_leaderboard,
    get_player_stats,
    get_rank,
    get_streak_leaderboard,
    record_game_results,
)
from game.questions import get_random_questions, resolve_pool_key
from game.server_progress import (
    check_and_auto_reset,
    get_asked_ids,
    mark_questions_asked,
    pool_progress,
)
from game.session import GameSession

log = logging.getLogger("sigmocatclash.game")

# ── Embed colours ──────────────────────────────────────────────────────────────
COL_START    = 0x5865F2   # Discord blurple
COL_QUESTION = 0xFF6B6B   # Warm coral
COL_RESULTS  = 0x57F287   # Success green
COL_FINAL    = 0xFEE75C   # Trophy gold
COL_ERROR    = 0xED4245   # Red
COL_STATS    = 0x9B59B6   # Purple — for stats/profile

# ── Decorative constants ───────────────────────────────────────────────────────
MEDALS = ["🥇", "🥈", "🥉"]
DIFF_EMOJIS = {"easy": "🟢", "medium": "🟡", "hard": "🔴", "all": "🌈", "daily": "📅"}

# Sentinel used when we need the full pool size unfiltered by server progress.
_ALL_QUESTIONS_LIMIT = 9999


# ── Helpers ────────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = 1024) -> str:
    return text[: limit - 3] + "..." if len(text) > limit else text


def _leaderboard_text(leaderboard: list[tuple[str, int]], max_entries: int = 8) -> str:
    if not leaderboard:
        return "No scores yet!"
    lines = []
    prev_score: Optional[int] = None
    rank = 0
    for i, (name, score) in enumerate(leaderboard[:max_entries]):
        if score != prev_score:
            rank = i + 1
            prev_score = score
        medal = MEDALS[rank - 1] if rank <= 3 else f"**#{rank}**"
        suffix = "pt" if score == 1 else "pts"
        lines.append(f"{medal} **{name}** — {score} {suffix}")
    return "\n".join(lines)


def _rank_badge(total_score: int) -> str:
    """Return a compact rank badge e.g. '💎 Master'."""
    name, emoji, _ = get_rank(total_score)
    return f"{emoji} {name}"


def _missing_permissions(channel: discord.TextChannel, me: discord.Member) -> list[str]:
    perms = channel.permissions_for(me)
    missing = []
    if not perms.send_messages:
        missing.append("Send Messages")
    if not perms.embed_links:
        missing.append("Embed Links")
    if not perms.add_reactions:
        missing.append("Add Reactions")
    return missing


_GUILD_ONLY_MSG = "❌ This command can only be used inside a server."


# ── Cog ────────────────────────────────────────────────────────────────────────

class SigmoCatClash(commands.Cog):
    """Core Category Clash game commands and message listener."""

    def __init__(self, bot: discord.Bot) -> None:
        self.bot = bot
        self._sessions: dict[int, GameSession] = {}  # channel_id → session

    # ── Slash commands ─────────────────────────────────────────────────────────

    @commands.slash_command(name="play", description="🎮 Start a SigmoCatClash — Category Clash game!")
    async def play(
        self,
        ctx: discord.ApplicationContext,
        rounds: discord.Option(int, "Number of rounds (1–10)", min_value=1, max_value=10) = 5,
        difficulty: discord.Option(
            str,
            "Question difficulty (daily = today's themed pack)",
            choices=["easy", "medium", "hard", "all", "daily"],
        ) = "all",
    ) -> None:
        # ── Guild-only guard ───────────────────────────────────────────────────
        if not ctx.guild_id:
            await ctx.respond(_GUILD_ONLY_MSG, ephemeral=True)
            return

        channel_id = ctx.channel_id
        guild_id   = ctx.guild_id

        # Defensive clamp — Discord enforces min/max, but be explicit
        rounds = max(1, min(rounds, 10))

        # Guard: game already running in this channel
        if channel_id in self._sessions and self._sessions[channel_id].is_active:
            await ctx.respond(
                "⚠️ A game is already running here! Use `/stop` to end it first.",
                ephemeral=True,
            )
            return

        # Guard: missing bot permissions
        if isinstance(ctx.channel, discord.TextChannel):
            missing = _missing_permissions(ctx.channel, ctx.guild.me)
            if missing:
                await ctx.respond(
                    f"❌ I'm missing permissions: **{', '.join(missing)}**\n"
                    "Please grant these and try again.",
                    ephemeral=True,
                )
                return

        pool_key = resolve_pool_key(difficulty)

        # ── Async server-progress check (auto-reset if pool exhausted) ─────────
        total_in_pool = len(get_random_questions(_ALL_QUESTIONS_LIMIT, difficulty))
        await check_and_auto_reset(guild_id, pool_key, total_in_pool)
        asked_ids = await get_asked_ids(guild_id, pool_key)

        questions = get_random_questions(rounds, difficulty, exclude_ids=asked_ids)
        if not questions:
            await ctx.respond(
                "❌ No questions found! Make sure `data/questions/` has CSV files.",
                ephemeral=True,
            )
            return

        actual_rounds = min(rounds, len(questions))
        questions = questions[:actual_rounds]

        session = GameSession(
            channel_id, ctx.author.id, actual_rounds,
            guild_id=guild_id, pool_key=pool_key,
        )
        self._sessions[channel_id] = session

        # Pool progress display
        asked_count, total_count = await pool_progress(guild_id, pool_key, total_in_pool)
        remaining = total_count - asked_count
        progress_line = ""
        if total_count > 0:
            progress_line = (
                f"\n📚 **Pool progress:** {asked_count}/{total_count} questions played"
                f" — {remaining} fresh remaining"
            )
            if asked_count == 0:
                progress_line += " *(pool just reset!)*"

        daily_label = ""
        if difficulty == "daily":
            daily_label = f"\n📅 **Today's pool:** {pool_key.capitalize()} questions"

        embed = discord.Embed(
            title="🐱⚡  SIGMOCATCLASH — Category Clash!",
            description=(
                "**Race to name things in the category before time runs out!**\n\n"
                f"📋 **Rounds:** {actual_rounds}\n"
                f"🎯 **Difficulty:** {difficulty.capitalize()}"
                f"{daily_label}\n"
                f"⏱️ **Time per round:** 60 seconds"
                f"{progress_line}\n\n"
                "**How to play:**\n"
                "> A **category** and **letter** (or *Any Letter* ✨) are posted each round.\n"
                "> Type things from that category (starting with the letter if given).\n"
                "> Use **commas** for multiple answers: `Bowl, Bread, Butter`\n"
                "> **First** to claim an answer scores the point — no repeats!\n"
                "> ⚡ Speed bonus for first scorer  •  🔥 Streak bonus from 3rd answer\n\n"
                "🚀 **First round starts in 5 seconds…**"
            ),
            color=COL_START,
        )
        embed.set_footer(text=f"Game started by {ctx.author.display_name}  •  Good luck!")
        await ctx.respond(embed=embed)

        task = asyncio.create_task(
            self._run_game(ctx.channel, session, questions),
            name=f"game-{channel_id}",
        )
        session.game_task = task

    # ──────────────────────────────────────────────────────────────────────────

    async def launch_from_interaction(
        self,
        interaction: discord.Interaction,
        rounds: int = 5,
        difficulty: str = "all",
    ) -> None:
        """Start a game from a button interaction (e.g. daily reminder).

        Uses defer() → followup pattern so Discord's 3-second acknowledgement
        window is never exceeded, even if DB calls take time.
        """
        guild_id = interaction.guild_id
        channel  = interaction.channel

        if not guild_id or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Games can only be started in a server text channel.",
                ephemeral=True,
            )
            return

        channel_id = channel.id
        rounds = max(1, min(rounds, 10))

        if channel_id in self._sessions and self._sessions[channel_id].is_active:
            await interaction.response.send_message(
                "⚠️ A game is already running here! Use `/stop` to end it first.",
                ephemeral=True,
            )
            return

        missing = _missing_permissions(channel, interaction.guild.me)
        if missing:
            await interaction.response.send_message(
                f"❌ I'm missing permissions: **{', '.join(missing)}**\n"
                "Please grant these and try again.",
                ephemeral=True,
            )
            return

        # Acknowledge within Discord's 3-second window; DB work happens after.
        await interaction.response.defer()

        pool_key = resolve_pool_key(difficulty)
        total_in_pool = len(get_random_questions(_ALL_QUESTIONS_LIMIT, difficulty))
        await check_and_auto_reset(guild_id, pool_key, total_in_pool)
        asked_ids = await get_asked_ids(guild_id, pool_key)

        questions = get_random_questions(rounds, difficulty, exclude_ids=asked_ids)
        if not questions:
            await interaction.followup.send(
                "❌ No questions found! Make sure `data/questions/` has CSV files.",
                ephemeral=True,
            )
            return

        actual_rounds = min(rounds, len(questions))
        questions = questions[:actual_rounds]

        initiator = interaction.user
        session = GameSession(
            channel_id, initiator.id, actual_rounds,
            guild_id=guild_id, pool_key=pool_key,
        )
        self._sessions[channel_id] = session

        asked_count, total_count = await pool_progress(guild_id, pool_key, total_in_pool)
        remaining = total_count - asked_count
        progress_line = ""
        if total_count > 0:
            progress_line = (
                f"\n📚 **Pool progress:** {asked_count}/{total_count} questions played"
                f" — {remaining} fresh remaining"
            )
            if asked_count == 0:
                progress_line += " *(pool just reset!)*"

        embed = discord.Embed(
            title="🐱⚡  SIGMOCATCLASH — Category Clash!",
            description=(
                "**Race to name things in the category before time runs out!**\n\n"
                f"📋 **Rounds:** {actual_rounds}\n"
                f"🎯 **Difficulty:** {difficulty.capitalize()}\n"
                f"⏱️ **Time per round:** 60 seconds"
                f"{progress_line}\n\n"
                "**How to play:**\n"
                "> A **category** and **letter** (or *Any Letter* ✨) are posted each round.\n"
                "> Type things from that category (starting with the letter if given).\n"
                "> Use **commas** for multiple answers: `Bowl, Bread, Butter`\n"
                "> **First** to claim an answer scores the point — no repeats!\n"
                "> ⚡ Speed bonus for first scorer  •  🔥 Streak bonus from 3rd answer\n\n"
                "🚀 **First round starts in 5 seconds…**"
            ),
            color=COL_START,
        )
        embed.set_footer(text=f"Game started by {initiator.display_name}  •  Good luck!")

        await interaction.followup.send(embed=embed)

        task = asyncio.create_task(
            self._run_game(channel, session, questions),
            name=f"game-{channel_id}",
        )
        session.game_task = task

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(name="stop", description="🛑 Stop the current game (host or mod only)")
    async def stop(self, ctx: discord.ApplicationContext) -> None:
        session = self._sessions.get(ctx.channel_id)
        if not session or not session.is_active:
            await ctx.respond("No game is running in this channel.", ephemeral=True)
            return

        is_host = ctx.author.id == session.host_id
        # ctx.author is a Member inside guilds; isinstance guard prevents AttributeError in DMs
        is_mod = (
            isinstance(ctx.author, discord.Member)
            and ctx.author.guild_permissions.manage_channels
        )
        if not (is_host or is_mod):
            await ctx.respond(
                "⚠️ Only the game host or a moderator can stop the game.", ephemeral=True
            )
            return

        session.stop()
        embed = discord.Embed(
            title="🛑 Game Stopped",
            description=f"Game stopped by {ctx.author.mention}.",
            color=COL_ERROR,
        )
        await ctx.respond(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(name="scores", description="📊 Show the current game scores")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def scores(self, ctx: discord.ApplicationContext) -> None:
        session = self._sessions.get(ctx.channel_id)
        if not session or not session.is_active:
            await ctx.respond("No active game in this channel.", ephemeral=True)
            return

        lb = session.get_leaderboard()
        embed = discord.Embed(
            title=f"📊 Scores — Round {session.current_round}/{session.total_rounds}",
            description=_leaderboard_text(lb),
            color=COL_FINAL,
        )
        await ctx.respond(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(name="rules", description="📖 How to play SigmoCatClash")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def rules(self, ctx: discord.ApplicationContext) -> None:
        embed = discord.Embed(
            title="📖  SigmoCatClash — How to Play",
            description=(
                "**Category Clash — Rapid Fire Edition**\n\n"
                "**Each round:**\n"
                "1️⃣  A **category** (e.g. *Things in a kitchen*) and a **letter** (e.g. *B*) appear.\n"
                "     Some rounds show **Any Letter ✨** — type any matching item!\n"
                "2️⃣  Type words from that category (starting with the given letter) in chat.\n"
                "3️⃣  Use **commas** for multiple answers at once: `Bowl, Bread, Butter`\n"
                "4️⃣  **First** to claim a valid answer gets **+1 point** — no one else can take it!\n"
                "5️⃣  Answers must be real items in the category (typos OK — fuzzy matching is on!).\n"
                "6️⃣  You have **60 seconds** — be quick!\n\n"
                "**Scoring bonuses:**\n"
                "⚡ **Speed bonus** — +1 pt to the very first scorer of each round\n"
                "🔥 **Streak bonus** — +1 pt for your 3rd+ consecutive valid answer in a round\n"
                "   (invalid answers reset your streak; duplicates don't)\n\n"
                "**Difficulty modes:**\n"
                "`easy` / `medium` / `hard` / `all` — classic difficulty pools\n"
                "`daily` — today's themed pack (India, Kerala, World…)\n\n"
                "**Reactions:**\n"
                "• ✅ = scored!  • ⚡ = speed bonus!  • 🔥 = streak!\n"
                "• 🔁 = already taken  • ❌ = not a valid answer\n\n"
                "**Rank system:**\n"
                "🪨 Rookie → 🥉 Player → 🥈 Pro → 🥇 Elite → 💎 Master → 👑 Legend\n"
                "Earn rank by accumulating total points across all games!\n\n"
                "**Commands:**\n"
                "`/play [rounds] [difficulty]` — Start a game\n"
                "`/scores` — Check live game scores\n"
                "`/leaderboard [view]` — Top players by score, daily streak, or wins\n"
                "`/mystats [user]` — Your all-time stats, rank & achievements\n"
                "`/stop` — End game (host / mods only)\n"
                "`/rules` — Show this message"
            ),
            color=COL_START,
        )
        await ctx.respond(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(name="leaderboard", description="🏆 Show the all-time leaderboard for this server")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def leaderboard(
        self,
        ctx: discord.ApplicationContext,
        view: discord.Option(
            str,
            "What to rank by",
            choices=["scores", "streaks", "wins"],
        ) = "scores",
    ) -> None:
        if not ctx.guild_id:
            await ctx.respond(_GUILD_ONLY_MSG, ephemeral=True)
            return

        guild_id = ctx.guild_id

        # ── Streaks view ───────────────────────────────────────────────────────
        if view == "streaks":
            entries = await get_streak_leaderboard(top_n=15, guild_id=guild_id)
            if not entries:
                embed = discord.Embed(
                    title="🔥  SigmoCatClash — Daily Streak Leaderboard",
                    description="No active streaks yet! Play daily to build yours.",
                    color=COL_FINAL,
                )
                await ctx.respond(embed=embed)
                return

            lines = []
            for i, entry in enumerate(entries):
                medal = MEDALS[i] if i < 3 else f"**#{i + 1}**"
                name    = entry.get("name", "Unknown")
                streak  = entry.get("current_streak", 0)
                longest = entry.get("longest_streak", 0)
                badge   = _rank_badge(entry.get("total_score", 0))
                lines.append(
                    f"{medal} **{name}** — 🔥 **{streak}-day** streak"
                    f"  *(longest: {longest}d  •  {badge})*"
                )

            embed = discord.Embed(
                title="🔥  SigmoCatClash — Daily Streak Leaderboard",
                description="\n".join(lines),
                color=COL_FINAL,
            )
            embed.set_footer(text="Play every day to keep your streak alive!  •  /leaderboard scores")
            await ctx.respond(embed=embed)
            return

        # ── Wins view ──────────────────────────────────────────────────────────
        if view == "wins":
            all_entries = await get_overall_leaderboard(top_n=100, guild_id=guild_id)
            entries = sorted(all_entries, key=lambda x: x.get("wins", 0), reverse=True)[:15]
            if not entries or entries[0].get("wins", 0) == 0:
                embed = discord.Embed(
                    title="🥇  SigmoCatClash — Most Wins",
                    description="No wins recorded yet! Play to score your first W.",
                    color=COL_FINAL,
                )
                await ctx.respond(embed=embed)
                return

            lines = []
            prev_wins: Optional[int] = None
            rank = 0
            for i, entry in enumerate(entries):
                wins = entry.get("wins", 0)
                if wins != prev_wins:
                    rank = i + 1
                    prev_wins = wins
                medal    = MEDALS[rank - 1] if rank <= 3 else f"**#{rank}**"
                name     = entry.get("name", "Unknown")
                games    = entry.get("games_played", 0)
                badge    = _rank_badge(entry.get("total_score", 0))
                win_rate = f"{wins / games * 100:.0f}%" if games > 0 else "—"
                suffix   = "win" if wins == 1 else "wins"
                lines.append(
                    f"{medal} **{name}** — **{wins}** {suffix}"
                    f"  *({win_rate} rate  •  {badge})*"
                )

            embed = discord.Embed(
                title="🥇  SigmoCatClash — Most Wins",
                description="\n".join(lines),
                color=COL_FINAL,
            )
            embed.set_footer(text="Top scorer wins the game  •  /leaderboard scores")
            await ctx.respond(embed=embed)
            return

        # ── Scores view (default) ──────────────────────────────────────────────
        entries = await get_overall_leaderboard(top_n=15, guild_id=guild_id)
        if not entries:
            embed = discord.Embed(
                title="🏆  SigmoCatClash — All-Time Leaderboard",
                description="No completed games yet! Use `/play` to start one.",
                color=COL_FINAL,
            )
            await ctx.respond(embed=embed)
            return

        lines = []
        prev_score: Optional[int] = None
        rank = 0
        for i, entry in enumerate(entries):
            score = entry.get("total_score", 0)
            if score != prev_score:
                rank = i + 1
                prev_score = score
            medal      = MEDALS[rank - 1] if rank <= 3 else f"**#{rank}**"
            name       = entry.get("name", "Unknown")
            games      = entry.get("games_played", 0)
            best       = entry.get("best_score", 0)
            streak     = entry.get("current_streak", 0)
            badge      = _rank_badge(score)
            suffix     = "pt" if score == 1 else "pts"
            game_str   = "game" if games == 1 else "games"
            streak_str = f"  🔥×{streak}" if streak >= 2 else ""
            lines.append(
                f"{medal} **{name}** — {score} {suffix}  [{badge}]{streak_str}\n"
                f"    *(best: {best} pts  •  {games} {game_str})*"
            )

        embed = discord.Embed(
            title="🏆  SigmoCatClash — All-Time Leaderboard",
            description="\n".join(lines),
            color=COL_FINAL,
        )
        embed.set_footer(
            text="Scores accumulate across all games  •  "
                 "/leaderboard streaks  •  /leaderboard wins"
        )
        await ctx.respond(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(
        name="mystats",
        description="📊 View your all-time stats, rank & achievements for this server",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def mystats(
        self,
        ctx: discord.ApplicationContext,
        user: discord.Option(
            discord.Member,
            "Player to look up (leave blank for yourself)",
            required=False,
        ) = None,
    ) -> None:
        if not ctx.guild_id:
            await ctx.respond(_GUILD_ONLY_MSG, ephemeral=True)
            return

        guild_id = ctx.guild_id
        target   = user or ctx.author
        stats    = await get_player_stats(target.id, guild_id=guild_id)

        if not stats:
            msg = (
                "You haven't played any games in this server yet! Start one with `/play`."
                if target.id == ctx.author.id
                else f"**{target.display_name}** hasn't played any games in this server yet."
            )
            await ctx.respond(msg, ephemeral=True)
            return

        total_score  = stats.get("total_score", 0)
        games_played = stats.get("games_played", 0)
        wins         = stats.get("wins", 0)
        best_score   = stats.get("best_score", 0)
        c_streak     = stats.get("current_streak", 0)
        l_streak     = stats.get("longest_streak", 0)
        total_ans    = stats.get("total_answers", 0)
        achievements = stats.get("achievements", [])

        rank_name, rank_emoji, next_thresh = get_rank(total_score)
        win_rate  = f"{wins / games_played * 100:.0f}%" if games_played > 0 else "—"
        avg_score = f"{total_score / games_played:.1f}"  if games_played > 0 else "—"

        embed = discord.Embed(
            title=f"📊  {target.display_name}'s Server Stats",
            color=COL_STATS,
        )

        # Rank badge + progress to next tier
        rank_line = f"**{rank_emoji} {rank_name}**"
        if next_thresh > 0:
            rank_line += f"  *(+{next_thresh - total_score} pts to next rank)*"
        else:
            rank_line += "  *(max rank — Legend!)*"
        embed.add_field(name="🏅 Rank", value=rank_line, inline=False)

        # Core stats grid
        embed.add_field(name="⭐ Total Score",   value=f"**{total_score}** pts",  inline=True)
        embed.add_field(name="🎮 Games Played",  value=f"**{games_played}**",     inline=True)
        embed.add_field(name="🏆 Wins",          value=f"**{wins}** ({win_rate})", inline=True)
        embed.add_field(name="🔝 Best Game",     value=f"**{best_score}** pts",   inline=True)
        embed.add_field(name="📝 Avg/Game",      value=f"**{avg_score}** pts",    inline=True)
        embed.add_field(name="✅ Answers Given", value=f"**{total_ans}**",        inline=True)

        # Daily streak with scaling fire
        streak_fire = "🔥" * min(c_streak // 3 + 1, 3) if c_streak >= 1 else ""
        streak_cur  = (
            f"**{c_streak}** day{'s' if c_streak != 1 else ''} {streak_fire}"
            if c_streak >= 1 else "**0** (play today to start!)"
        )
        embed.add_field(
            name="🔥 Daily Streak",
            value=f"Current: {streak_cur}\nLongest: **{l_streak}** days",
            inline=True,
        )

        # Achievements
        ach_parts = [
            f"{ACHIEVEMENTS[aid]['emoji']} {ACHIEVEMENTS[aid]['name']}"
            for aid in achievements
            if aid in ACHIEVEMENTS
        ]
        if ach_parts:
            embed.add_field(
                name=f"🏅 Achievements ({len(ach_parts)})",
                value=_truncate("  ·  ".join(ach_parts), 512),
                inline=False,
            )
        else:
            embed.add_field(
                name="🏅 Achievements",
                value="*None yet — play games to unlock badges!*",
                inline=False,
            )

        embed.set_footer(
            text="Stats are server-specific  •  /leaderboard to compare with others"
        )
        await ctx.respond(embed=embed)

    # ── Message listener ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots and DMs — games only run in guild channels
        if message.author.bot or not message.guild:
            return

        session = self._sessions.get(message.channel.id)
        if not session or not session.accepting_answers:
            return

        raw = message.content.strip()
        if not raw:
            return

        # Cap at 6 comma-separated answers per message to curb spam
        parts = [p.strip() for p in raw.split(",") if p.strip()][:6]
        if not parts:
            return

        player_id   = message.author.id
        player_name = message.author.display_name

        scored_new  = False
        got_dup     = False
        got_invalid = False
        got_streak  = False
        got_speed   = False

        seen_this_msg: set[str] = set()
        for part in parts:
            normalised = part.lower()
            if normalised in seen_this_msg:
                continue
            seen_this_msg.add(normalised)

            result = session.submit_answer(player_id, player_name, part)

            if result.is_valid and not result.is_duplicate and result.points > 0:
                scored_new = True
                if result.streak_bonus:
                    got_streak = True
                if result.speed_bonus:
                    got_speed = True
            elif result.is_duplicate:
                got_dup = True
            elif not result.is_valid:
                got_invalid = True

        try:
            if scored_new:
                await message.add_reaction("✅")
                if got_speed:
                    await message.add_reaction("⚡")
                if got_streak:
                    await message.add_reaction("🔥")
            elif got_dup and not scored_new:
                await message.add_reaction("🔁")
            elif got_invalid and not scored_new and not got_dup:
                q_letter   = (session.current_question or {}).get("letter", "")
                any_starts = (
                    any(len(p.strip()) >= 3 for p in parts)
                    if q_letter == "*"
                    else any(
                        p.lower().startswith(q_letter.lower())
                        for p in parts if q_letter
                    )
                )
                if any_starts:
                    await message.add_reaction("❌")
        except (discord.HTTPException, discord.Forbidden):
            pass

    # ── Game loop ──────────────────────────────────────────────────────────────

    async def _run_game(
        self,
        channel: discord.TextChannel,
        session: GameSession,
        questions: list[dict],
    ) -> None:
        asked_ids: list[str] = []
        try:
            await asyncio.sleep(5)  # Pre-game countdown

            for i, question in enumerate(questions):
                if not session.is_active:
                    break
                await self._run_round(channel, session, question)
                qid = question.get("id", "")
                if qid:
                    asked_ids.append(qid)
                if not session.is_active:
                    break
                if i < len(questions) - 1:
                    await asyncio.sleep(5)

            if session.is_active:
                await self._post_final(channel, session)

        except asyncio.CancelledError:
            log.info("Game in channel %s was cancelled.", channel.id)
        except Exception:
            log.exception("Unexpected error in game loop for channel %s.", channel.id)
            try:
                await channel.send("❌ An unexpected error ended the game early.")
            except Exception:
                pass
        finally:
            if session.guild_id and asked_ids:
                try:
                    await mark_questions_asked(session.guild_id, asked_ids, session.pool_key)
                except Exception:
                    log.exception("Failed to persist server progress.")
            session.is_active = False
            self._sessions.pop(channel.id, None)

    # ──────────────────────────────────────────────────────────────────────────

    async def _run_round(
        self,
        channel: discord.TextChannel,
        session: GameSession,
        question: dict,
    ) -> None:
        time_limit: int = question.get("time_limit", 60)
        session.start_round(question)

        letter     = question["letter"]
        category   = question["category"]
        difficulty = question.get("difficulty", "medium")
        emoji      = question.get("emoji", "📂")
        hint       = question.get("hint", "")
        diff_emoji = DIFF_EMOJIS.get(difficulty, "🟡")

        any_letter        = letter == "*"
        letter_display    = "**Any Letter ✨**" if any_letter else f"# {letter}"
        letter_field_name = "🔤  Letter" if any_letter else "🔤  Starting Letter"

        embed = discord.Embed(
            title=f"Round {session.current_round} of {session.total_rounds}  •  {emoji} Category Clash",
            color=COL_QUESTION,
        )
        embed.add_field(name="📂  Category",              value=f"## {category}", inline=False)
        embed.add_field(name=letter_field_name,           value=letter_display,   inline=True)
        embed.add_field(name="⏱️  Time Limit",            value=f"**{time_limit}s**", inline=True)
        embed.add_field(name=f"{diff_emoji}  Difficulty", value=difficulty.capitalize(), inline=True)
        if hint:
            embed.add_field(name="💡  Hint", value=hint, inline=False)
        embed.set_footer(
            text="⚡ Type answers now!  Multiple answers: use commas  →  Bowl, Bread, Butter"
        )

        try:
            await channel.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden) as exc:
            log.error("Failed to send question embed: %s", exc)
            session.end_round()
            return

        half          = time_limit // 2
        remaining     = time_limit - half
        letter_remind = "Any Letter" if any_letter else letter

        await asyncio.sleep(half)
        if not session.is_active:
            return

        try:
            await channel.send(
                f"⏰  **{remaining} seconds left!**  "
                f"Category: **{category}**  |  Letter: **{letter_remind}**",
                delete_after=float(remaining),
            )
        except (discord.HTTPException, discord.Forbidden):
            pass

        if remaining > 3:
            await asyncio.sleep(remaining - 3)
            if not session.is_active:
                return
            try:
                await channel.send("3️⃣  2️⃣  1️⃣  …", delete_after=4)
            except (discord.HTTPException, discord.Forbidden):
                pass
            await asyncio.sleep(3)
        else:
            await asyncio.sleep(remaining)

        session.end_round()
        await asyncio.sleep(0.3)  # tiny pause so last-second answers register
        await self._post_round_results(channel, session, question)

    # ──────────────────────────────────────────────────────────────────────────

    async def _post_round_results(
        self,
        channel: discord.TextChannel,
        session: GameSession,
        question: dict,
    ) -> None:
        summary           = session.get_round_summary()
        player_valid      = summary["player_valid"]
        player_points     = summary["player_points"]
        player_max_streak = summary["player_max_streak"]
        player_speed      = summary["player_speed"]
        duplicates        = summary["duplicates"]

        q_letter     = question.get("letter", "")
        letter_label = "Any Letter" if q_letter == "*" else q_letter

        embed = discord.Embed(
            title=f"⏱️  Time's Up!  —  Round {session.current_round} Results",
            description=(
                f"**Category:** {question['category']}  |  "
                f"**Letter:** {letter_label}"
            ),
            color=COL_RESULTS,
        )

        # Valid answers
        if player_valid:
            lines = []
            for player, answers in sorted(
                player_valid.items(),
                key=lambda x: player_points.get(x[0], 0),
                reverse=True,
            ):
                pts    = player_points.get(player, len(answers))
                suffix = "pt" if pts == 1 else "pts"
                tags: list[str] = []
                if player in player_speed:
                    tags.append("⚡ Speed")
                streak = player_max_streak.get(player, 0)
                if streak >= 3:
                    tags.append(f"🔥 ×{streak} streak")
                tag_str = "  " + "  ".join(tags) if tags else ""
                lines.append(
                    f"✅  **{player}** — {', '.join(answers)}  *(+{pts} {suffix})*{tag_str}"
                )
            embed.add_field(
                name="🎯  Claimed Answers",
                value=_truncate("\n".join(lines)),
                inline=False,
            )
        else:
            embed.add_field(
                name="🎯  Claimed Answers",
                value="Nobody scored this round — tougher than it looks! 😅",
                inline=False,
            )

        # Duplicates
        if duplicates:
            dup_names = sorted({a.player_name for a in duplicates})
            embed.add_field(
                name="🔁  Collisions",
                value=_truncate("Already taken: " + ", ".join(f"**{n}**" for n in dup_names[:10])),
                inline=False,
            )

        # Example answers from CSV
        expected = question.get("answers", [])[:10]
        if expected:
            embed.add_field(
                name="💡  Example Answers",
                value=_truncate("  •  ".join(expected)),
                inline=False,
            )

        # Running leaderboard (top 6) with close-race tension on last round
        lb = session.get_leaderboard()
        if lb:
            is_last = session.current_round >= session.total_rounds
            lb_title = (
                "📊  Final Standings" if is_last
                else f"📊  Standings — after Round {session.current_round}"
            )
            tension = ""
            if is_last and len(lb) >= 2 and lb[0][1] > 0:
                gap = lb[0][1] - lb[1][1]
                if gap == 0:
                    tension = "\n🤝 **Dead heat at the top!**"
                elif gap <= 2:
                    tension = f"\n⚡ **RAZOR CLOSE — only {gap} pt{'s' if gap != 1 else ''} in it!**"
            embed.add_field(
                name=lb_title,
                value=_truncate(_leaderboard_text(lb, max_entries=6) + tension),
                inline=False,
            )

        if session.current_round < session.total_rounds:
            embed.set_footer(
                text=f"Next round in 5 seconds…  Round {session.current_round + 1}/{session.total_rounds}"
            )

        try:
            await channel.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden) as exc:
            log.error("Failed to send round results: %s", exc)

    # ──────────────────────────────────────────────────────────────────────────

    async def _post_final(self, channel: discord.TextChannel, session: GameSession) -> None:
        guild_id = session.guild_id  # may be None for DM games (shouldn't happen post-guard)

        player_scores = {
            pid: (session.player_names.get(pid, "Unknown"), score)
            for pid, score in session.scores.items()
        }
        player_answers = dict(session.player_total_answers)

        # Persist and get newly earned achievements — guild_id scopes all writes
        new_achievements: dict[int, list[str]] = {}
        if guild_id:
            try:
                new_achievements = await record_game_results(
                    player_scores, player_answers, guild_id=guild_id
                )
            except Exception:
                log.exception("Failed to record game results for guild %s.", guild_id)

        lb = session.get_leaderboard()

        embed = discord.Embed(title="🏆  SIGMOCATCLASH — GAME OVER!", color=COL_FINAL)

        # Determine winner by player_id to avoid display-name collision
        winner_pid: Optional[int] = (
            max(session.scores, key=session.scores.get) if session.scores else None
        )
        winner_stats = (
            await get_player_stats(winner_pid, guild_id=guild_id)
            if winner_pid and guild_id else None
        )

        if not lb:
            embed.description = "Nobody played! 😢  Invite your friends next time!"
        elif len(lb) == 1:
            name, score = lb[0]
            badge = f"  {_rank_badge(winner_stats['total_score'])}" if winner_stats else ""
            embed.description = (
                f"🎉  **{name}** wins with **{score} {'pt' if score == 1 else 'pts'}**!{badge}\n"
                "Solo run! Challenge your friends next time. 💪"
            )
        else:
            winner_name, winner_score = lb[0]
            suffix = "pt" if winner_score == 1 else "pts"
            badge  = f"  {_rank_badge(winner_stats['total_score'])}" if winner_stats else ""
            embed.description = (
                f"🎉  **{winner_name}** takes the crown with **{winner_score} {suffix}**!"
                f"  👑{badge}\n\n"
                + _leaderboard_text(lb)
            )

        # Truncate description to Discord's 4096-char limit
        if len(embed.description) > 4093:
            embed.description = embed.description[:4090] + "..."

        # Game stats
        embed.add_field(
            name="📈  Game Stats",
            value=(
                f"**Rounds:** {session.current_round}\n"
                f"**Players:** {len(session.scores)}\n"
                f"**Total unique answers:** {session.total_unique_answers}"
            ),
            inline=False,
        )

        # Highlights: close race, per-player streak/rank-up/achievements
        highlight_lines: list[str] = []

        if len(lb) >= 2 and lb[0][1] > 0:
            gap = lb[0][1] - lb[1][1]
            if gap == 0:
                highlight_lines.append("🤝 **Dead heat at the top!** Incredible game!")
            elif gap <= 2:
                s = "pt" if gap == 1 else "pts"
                highlight_lines.append(f"⚡ **RAZOR CLOSE!** Only {gap} {s} between 1st and 2nd!")

        for pid, (name, score) in sorted(
            player_scores.items(), key=lambda x: x[1][1], reverse=True
        ):
            if not guild_id:
                continue
            stats = await get_player_stats(pid, guild_id=guild_id)
            if not stats:
                continue

            # Daily streak fire (only shown if >= 2 — "new" streak not highlighted)
            streak = stats.get("current_streak", 0)
            if streak >= 2:
                fire = "🔥" * min(streak // 3 + 1, 3)
                highlight_lines.append(f"{fire} **{name}** is on a **{streak}-day streak!**")

            # Rank-up notification
            old_total = max(stats.get("total_score", 0) - score, 0)
            old_rank, _, _ = get_rank(old_total)
            new_rank, new_emoji, next_thresh = get_rank(stats["total_score"])
            if old_rank != new_rank:
                highlight_lines.append(f"⬆️ **{name}** ranked up to **{new_emoji} {new_rank}**!")
            elif next_thresh > 0 and (next_thresh - stats["total_score"]) <= 15:
                pts_away = next_thresh - stats["total_score"]
                highlight_lines.append(
                    f"✨ **{name}** is just **{pts_away} pts** from the next rank!"
                )

            # Achievement unlocks
            for ach_id in new_achievements.get(pid, []):
                ach = ACHIEVEMENTS.get(ach_id, {})
                if ach:
                    highlight_lines.append(
                        f"🏅 **{name}** unlocked **{ach['emoji']} {ach['name']}** — _{ach['desc']}_"
                    )

        if highlight_lines:
            embed.add_field(
                name="✨  Highlights",
                value=_truncate("\n".join(highlight_lines[:10])),
                inline=False,
            )

        embed.set_footer(
            text=(
                "Play again with /play  •  "
                "/mystats for your server profile  •  "
                "/leaderboard to climb the ranks  🐱⚡"
            )
        )

        try:
            await channel.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden) as exc:
            log.error("Failed to send final results: %s", exc)


# ── Extension entry point ──────────────────────────────────────────────────────

def setup(bot: discord.Bot) -> None:
    bot.add_cog(SigmoCatClash(bot))
