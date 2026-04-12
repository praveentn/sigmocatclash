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
from game.questions import get_random_questions, resolve_pool_key, get_daily_pool_key
from game.server_progress import mark_questions_asked, pool_progress
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
DIFF_EMOJIS = {"easy": "🟢", "medium": "🟡", "hard": "🔴", "all": "🌈", "daily": "📅"}
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
            "Question difficulty (daily = today's themed pack)",
            choices=["easy", "medium", "hard", "all", "daily"],
        ) = "all",
    ) -> None:
        channel_id = ctx.channel_id
        guild_id   = ctx.guild_id

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

        pool_key = resolve_pool_key(difficulty)

        questions = get_random_questions(rounds, difficulty, guild_id=guild_id)
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

        # Show pool progress for server-tracked pools
        progress_line = ""
        if guild_id:
            total_in_pool = len(get_random_questions(9999, difficulty))  # full unfiltered pool
            asked_count, total_count = pool_progress(guild_id, pool_key, total_in_pool)
            remaining = total_count - asked_count
            if total_count > 0:
                progress_line = (
                    f"\n📚 **Pool progress:** {asked_count}/{total_count} questions played"
                    f" — {remaining} fresh remaining"
                )
                if asked_count == 0:
                    progress_line += " *(pool just reset!)*"

        # Daily theme label
        daily_label = ""
        if difficulty == "daily":
            daily_label = f"\n📅 **Today's pool:** {pool_key.capitalize()} questions"

        # Announce game start
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
                "     Some rounds show **Any Letter ✨** — type any matching item!\n"
                "2️⃣  Type words from that category (starting with the given letter) in chat.\n"
                "3️⃣  Use **commas** for multiple answers at once: `Bowl, Bread, Butter`\n"
                "4️⃣  **First** to claim a valid answer gets **+1 point** — no one else can take it!\n"
                "5️⃣  Answers must be real items in the category (typos are OK — fuzzy matching is on!).\n"
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
        got_streak = False
        got_speed = False

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
                got_duplicate = True
            elif not result.is_valid:
                got_invalid = True

        # React to confirm receipt
        try:
            if scored_new:
                await message.add_reaction("✅")
                if got_speed:
                    await message.add_reaction("⚡")
                if got_streak:
                    await message.add_reaction("🔥")
            elif got_duplicate and not scored_new:
                await message.add_reaction("🔁")
            elif got_invalid and not scored_new and not got_duplicate:
                q_letter = (session.current_question or {}).get("letter", "")
                if q_letter == "*":
                    any_starts = any(len(p.strip()) >= 3 for p in parts)
                else:
                    any_starts = any(p.lower().startswith(q_letter.lower()) for p in parts if q_letter)
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
                # Track this question as asked for the server
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
            # Persist all asked question IDs for this server at once
            if session.guild_id and asked_ids:
                try:
                    mark_questions_asked(session.guild_id, asked_ids, session.pool_key)
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

        any_letter    = letter == "*"
        letter_display    = "**Any Letter ✨**" if any_letter else f"# {letter}"
        letter_field_name = "🔤  Letter" if any_letter else "🔤  Starting Letter"

        # ── Question embed ─────────────────────────────────────────────────────
        embed = discord.Embed(
            title=f"Round {session.current_round} of {session.total_rounds}  •  {emoji} Category Clash",
            color=COL_QUESTION,
        )
        embed.add_field(name="📂  Category",       value=f"## {category}", inline=False)
        embed.add_field(name=letter_field_name,    value=letter_display,   inline=True)
        embed.add_field(name="⏱️  Time Limit",     value=f"**{time_limit}s**", inline=True)
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
        letter_reminder = "Any Letter" if any_letter else letter

        await asyncio.sleep(half)
        if not session.is_active:
            return

        try:
            await channel.send(
                f"⏰  **{remaining} seconds left!**  Category: **{category}**  |  Letter: **{letter_reminder}**",
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
        summary          = session.get_round_summary()
        player_valid     = summary["player_valid"]
        player_points    = summary["player_points"]
        player_max_streak = summary["player_max_streak"]
        player_speed     = summary["player_speed"]
        duplicates       = summary["duplicates"]

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

        # ── Valid answers ──────────────────────────────────────────────────────
        if player_valid:
            lines = []
            for player, answers in sorted(
                player_valid.items(),
                key=lambda x: player_points.get(x[0], 0),
                reverse=True,
            ):
                pts = player_points.get(player, len(answers))
                suffix = "pt" if pts == 1 else "pts"
                answer_str = ", ".join(answers)

                tags: list[str] = []
                if player in player_speed:
                    tags.append("⚡ Speed")
                streak = player_max_streak.get(player, 0)
                if streak >= 3:
                    tags.append(f"🔥 ×{streak} streak")
                tag_str = "  " + "  ".join(tags) if tags else ""

                lines.append(f"✅  **{player}** — {answer_str}  *(+{pts} {suffix})*{tag_str}")
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
                "📊  Final Standings" if is_last
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
