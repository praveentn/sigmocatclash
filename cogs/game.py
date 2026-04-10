"""
SigmoCatClash — Category Clash game cog.

Flow per game:
  /play  →  5-second countdown  →  N rounds  →  final leaderboard

Each round:
  post question embed  →  accept text answers  →  half-time warning
  →  3-2-1 countdown  →  end_round  →  post results embed  →  5-sec gap
"""

import asyncio
import logging
from typing import Optional

import discord
from discord.ext import commands

from game.leaderboard import get_overall_leaderboard, record_game_results
from game.questions import get_random_questions
from game.session import GameSession

log = logging.getLogger("sigmocatclash.game")

# ── Embed colours ──────────────────────────────────────────────────────────────
COL_START    = 0x5865F2   # Discord blurple
COL_QUESTION = 0xFF6B6B   # Warm coral
COL_RESULTS  = 0x57F287   # Success green
COL_FINAL    = 0xFEE75C   # Trophy gold
COL_ERROR    = 0xED4245   # Red

# ── Decorative constants ───────────────────────────────────────────────────────
MEDALS = ["🥇", "🥈", "🥉"]
DIFF_EMOJIS = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}
FIRSTS = ["⚡", "🔥", "💥", "✨", "🎯"]  # cycling first-answer indicators


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
            "Question difficulty",
            choices=["easy", "medium", "hard", "all"],
        ) = "all",
    ) -> None:
        channel_id = ctx.channel_id

        # Guard: game already running
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
                    f"❌ I'm missing permissions: **{', '.join(missing)}**\nPlease grant these and try again.",
                    ephemeral=True,
                )
                return

        questions = get_random_questions(rounds, difficulty)
        if not questions:
            await ctx.respond(
                "❌ No questions found! Make sure `data/questions/` has CSV files.",
                ephemeral=True,
            )
            return

        actual_rounds = min(rounds, len(questions))
        questions = questions[:actual_rounds]

        session = GameSession(channel_id, ctx.author.id, actual_rounds)
        self._sessions[channel_id] = session

        # Announce game start
        embed = discord.Embed(
            title="🐱⚡  SIGMOCATCLASH — Category Clash!",
            description=(
                "**Race to name things in the category before time runs out!**\n\n"
                f"📋 **Rounds:** {actual_rounds}\n"
                f"🎯 **Difficulty:** {difficulty.capitalize()}\n"
                f"⏱️ **Time per round:** 60 seconds\n\n"
                "**How to play:**\n"
                "> A **category** and **letter** are posted each round.\n"
                "> Type things from that category starting with that letter.\n"
                "> Use **commas** for multiple answers: `Bowl, Bread, Butter`\n"
                "> **First** to claim an answer scores the point — no repeats!\n\n"
                "🚀 **First round starts in 5 seconds…**"
            ),
            color=COL_START,
        )
        embed.set_footer(text=f"Game started by {ctx.author.display_name}  •  Good luck!")
        await ctx.respond(embed=embed)

        # Kick off the game loop as a background task
        task = asyncio.create_task(
            self._run_game(ctx.channel, session, questions),
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
        is_mod = isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.manage_channels
        if not (is_host or is_mod):
            await ctx.respond("⚠️ Only the game host or a moderator can stop the game.", ephemeral=True)
            return

        session.stop()
        # _run_game's finally block will pop the session
        embed = discord.Embed(
            title="🛑 Game Stopped",
            description=f"Game stopped by {ctx.author.mention}.",
            color=COL_ERROR,
        )
        await ctx.respond(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(name="scores", description="📊 Show the current game scores")
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
    async def rules(self, ctx: discord.ApplicationContext) -> None:
        embed = discord.Embed(
            title="📖  SigmoCatClash — How to Play",
            description=(
                "**Category Clash — Rapid Fire Edition**\n\n"
                "**Each round:**\n"
                "1️⃣  A **category** (e.g. *Things in a kitchen*) and a **letter** (e.g. *B*) appear.\n"
                "2️⃣  Type words from that category starting with that letter in chat.\n"
                "3️⃣  Use **commas** for multiple answers at once: `Bowl, Bread, Butter`\n"
                "4️⃣  **First** to claim a valid answer gets **+1 point** — no one else can take it!\n"
                "5️⃣  Answers must be real items in the category (typos are OK — fuzzy matching is on!).\n"
                "6️⃣  You have **60 seconds** — be quick!\n\n"
                "**Scoring:**\n"
                "• +1 pt per unique valid answer you claim first\n"
                "• ✅ Reaction = claimed! Points added.\n"
                "• 🔁 Reaction = already taken by someone else\n"
                "• ❌ Reaction = not a valid answer for this category\n\n"
                "**Commands:**\n"
                "`/play [rounds] [difficulty]` — Start a game\n"
                "`/scores` — Check live game scores\n"
                "`/leaderboard` — Show all-time leaderboard\n"
                "`/stop` — End game (host / mods only)\n"
                "`/rules` — Show this message"
            ),
            color=COL_START,
        )
        await ctx.respond(embed=embed)

    # ──────────────────────────────────────────────────────────────────────────

    @commands.slash_command(name="leaderboard", description="🏆 Show the all-time overall leaderboard")
    async def leaderboard(self, ctx: discord.ApplicationContext) -> None:
        entries = get_overall_leaderboard(top_n=15)

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
            medal = MEDALS[rank - 1] if rank <= 3 else f"**#{rank}**"
            name = entry.get("name", "Unknown")
            games = entry.get("games_played", 0)
            best = entry.get("best_score", 0)
            suffix = "pt" if score == 1 else "pts"
            game_str = "game" if games == 1 else "games"
            lines.append(
                f"{medal} **{name}** — {score} {suffix} total"
                f"  *(best: {best} pts, {games} {game_str})*"
            )

        embed = discord.Embed(
            title="🏆  SigmoCatClash — All-Time Leaderboard",
            description="\n".join(lines),
            color=COL_FINAL,
        )
        embed.set_footer(text="Scores accumulate across all completed games  •  Play again with /play!")
        await ctx.respond(embed=embed)

    # ── Message listener ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots and DMs
        if message.author.bot or not message.guild:
            return

        session = self._sessions.get(message.channel.id)
        if not session or not session.accepting_answers:
            return

        raw = message.content.strip()
        if not raw:
            return

        # Parse comma-separated answers; cap at 6 per message to curb spam
        parts = [p.strip() for p in raw.split(",") if p.strip()][:6]
        if not parts:
            return

        player_id = message.author.id
        player_name = message.author.display_name

        scored_new = False
        got_duplicate = False
        got_invalid = False

        # Deduplicate within this single message before submission
        seen_this_msg: set[str] = set()
        for part in parts:
            normalised = part.lower()
            if normalised in seen_this_msg:
                continue
            seen_this_msg.add(normalised)

            result = session.submit_answer(player_id, player_name, part)

            if result.is_valid and not result.is_duplicate and result.points > 0:
                scored_new = True
            elif result.is_duplicate:
                got_duplicate = True
            elif not result.is_valid:
                got_invalid = True

        # React to confirm receipt — one reaction only per message
        try:
            if scored_new:
                await message.add_reaction("✅")
            elif got_duplicate and not scored_new:
                await message.add_reaction("🔁")
            elif got_invalid and not scored_new and not got_duplicate:
                # Only react with ❌ if EVERY part was invalid (bad letter / too short)
                # so normal chat during a game doesn't get spammed with ❌
                letter = (session.current_question or {}).get("letter", "")
                any_starts = any(p.lower().startswith(letter.lower()) for p in parts if letter)
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
        try:
            await asyncio.sleep(5)  # Pre-game countdown

            for i, question in enumerate(questions):
                if not session.is_active:
                    break
                await self._run_round(channel, session, question)
                if not session.is_active:
                    break
                # Gap between rounds (skip after last)
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

        # ── Question embed ─────────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"Round {session.current_round} of {session.total_rounds}  •  {emoji} Category Clash",
            color=COL_QUESTION,
        )
        embed.add_field(name="📂  Category", value=f"## {category}", inline=False)
        embed.add_field(name="🔤  Starting Letter", value=f"# {letter}", inline=True)
        embed.add_field(name="⏱️  Time Limit",      value=f"**{time_limit}s**", inline=True)
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

        # ── Timer ──────────────────────────────────────────────────────────────
        half = time_limit // 2
        remaining = time_limit - half

        await asyncio.sleep(half)
        if not session.is_active:
            return

        try:
            await channel.send(
                f"⏰  **{remaining} seconds left!**  Category: **{category}**  |  Letter: **{letter}**",
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
        summary   = session.get_round_summary()
        player_valid: dict[str, list[str]] = summary["player_valid"]
        duplicates = summary["duplicates"]

        embed = discord.Embed(
            title=f"⏱️  Time's Up!  —  Round {session.current_round} Results",
            description=(
                f"**Category:** {question['category']}  |  "
                f"**Letter:** {question['letter']}"
            ),
            color=COL_RESULTS,
        )

        # ── Valid answers ──────────────────────────────────────────────────────
        if player_valid:
            lines = []
            # Sort by number of answers (most productive player first)
            for player, answers in sorted(player_valid.items(), key=lambda x: len(x[1]), reverse=True):
                pts = len(answers)
                answer_str = ", ".join(answers)
                suffix = "pt" if pts == 1 else "pts"
                lines.append(f"✅  **{player}** — {answer_str}  *(+{pts} {suffix})*")
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

        # ── Duplicates note ────────────────────────────────────────────────────
        if duplicates:
            dup_players = sorted({a.player_name for a in duplicates})
            dup_text = "Already taken: " + ", ".join(f"**{n}**" for n in dup_players[:10])
            embed.add_field(name="🔁  Collisions", value=_truncate(dup_text), inline=False)

        # ── Example answers from CSV (educational / fun) ───────────────────────
        expected = question.get("answers", [])[:10]
        if expected:
            examples = "  •  ".join(expected)
            embed.add_field(
                name="💡  Example Answers",
                value=_truncate(examples),
                inline=False,
            )

        # ── Running leaderboard (top 6) ────────────────────────────────────────
        lb = session.get_leaderboard()
        if lb:
            is_last = session.current_round >= session.total_rounds
            lb_title = (
                f"📊  Final Standings" if is_last
                else f"📊  Standings — after Round {session.current_round}"
            )
            embed.add_field(
                name=lb_title,
                value=_truncate(_leaderboard_text(lb, max_entries=6)),
                inline=False,
            )

        if session.current_round < session.total_rounds:
            embed.set_footer(text=f"Next round in 5 seconds…  Round {session.current_round + 1}/{session.total_rounds}")

        try:
            await channel.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden) as exc:
            log.error("Failed to send round results: %s", exc)

    # ──────────────────────────────────────────────────────────────────────────

    async def _post_final(self, channel: discord.TextChannel, session: GameSession) -> None:
        # Persist scores to the overall leaderboard before displaying
        player_scores = {
            pid: (session.player_names.get(pid, "Unknown"), score)
            for pid, score in session.scores.items()
        }
        record_game_results(player_scores)

        lb = session.get_leaderboard()

        embed = discord.Embed(title="🏆  SIGMOCATCLASH — GAME OVER!", color=COL_FINAL)

        if not lb:
            embed.description = "Nobody played! 😢  Invite your friends next time!"
        elif len(lb) == 1:
            name, score = lb[0]
            embed.description = (
                f"🎉  **{name}** wins with **{score} {'pt' if score == 1 else 'pts'}**!\n"
                "Solo run! Challenge your friends next time. 💪"
            )
        else:
            name, score = lb[0]
            suffix = "pt" if score == 1 else "pts"
            embed.description = (
                f"🎉  **{name}** takes the crown with **{score} {suffix}**!  👑\n\n"
                + _leaderboard_text(lb)
            )

        # Truncate if leaderboard is very long
        if len(embed.description) > 4096:
            embed.description = embed.description[:4090] + "\n..."

        embed.add_field(
            name="📈  Game Stats",
            value=(
                f"**Rounds:** {session.current_round}\n"
                f"**Players:** {len(session.scores)}\n"
                f"**Total unique answers claimed:** {session.total_unique_answers}"
            ),
            inline=False,
        )
        embed.set_footer(text="Play again with /play  •  Thanks for playing SigmoCatClash! 🐱⚡")

        try:
            await channel.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden) as exc:
            log.error("Failed to send final results: %s", exc)


# ── Extension entry point ──────────────────────────────────────────────────────

def setup(bot: discord.Bot) -> None:
    bot.add_cog(SigmoCatClash(bot))
