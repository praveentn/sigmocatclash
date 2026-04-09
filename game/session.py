"""
GameSession — tracks all state for one active SigmoCatClash game in a channel.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RoundAnswer:
    player_id: int
    player_name: str
    text: str           # original casing from player
    is_valid: bool      # starts with letter + meets length check
    is_duplicate: bool  # another player already claimed this exact answer
    points: int
    timestamp: float


class GameSession:
    def __init__(self, channel_id: int, host_id: int, total_rounds: int = 5):
        self.channel_id = channel_id
        self.host_id = host_id
        self.total_rounds = total_rounds
        self.current_round = 0
        self.is_active = True
        self.accepting_answers = False

        # Persistent across rounds
        self.scores: dict[int, int] = {}          # player_id -> cumulative score
        self.player_names: dict[int, str] = {}    # player_id -> display_name
        self.total_unique_answers = 0             # game-wide stat

        # Per-round state — reset in start_round()
        self.current_question: Optional[dict] = None
        self.claimed_answers: set[str] = set()              # normalised answers claimed this round
        self.round_answers: list[RoundAnswer] = []          # all answer attempts this round
        self.player_round_answers: dict[int, set[str]] = {} # per-player dedup within round

        # Async handles
        self.game_task: Optional[asyncio.Task] = None

    # ── Player helpers ────────────────────────────────────────────

    def ensure_player(self, player_id: int, player_name: str) -> None:
        if player_id not in self.scores:
            self.scores[player_id] = 0
            self.player_names[player_id] = player_name
        # Always update display name (nick changes)
        self.player_names[player_id] = player_name

    # ── Round lifecycle ───────────────────────────────────────────

    def start_round(self, question: dict) -> None:
        self.current_round += 1
        self.current_question = question
        self.claimed_answers = set()
        self.round_answers = []
        self.player_round_answers = {}
        self.accepting_answers = True

    def end_round(self) -> None:
        self.accepting_answers = False

    # ── Answer submission ─────────────────────────────────────────

    def submit_answer(self, player_id: int, player_name: str, text: str) -> RoundAnswer:
        """
        Validate and record a single answer.

        Rules:
          1. Must start with the letter constraint (case-insensitive).
          2. Must be at least 3 characters (after stripping).
          3. No duplicate by the SAME player in this message batch.
          4. No global duplicate across players in this round.
        """
        self.ensure_player(player_id, player_name)

        cleaned = text.strip()
        normalised = cleaned.lower()
        letter = (self.current_question.get("letter") or "").lower()

        # ── Validation checks ─────────────────────────────────────
        if len(normalised) < 3:
            return self._make_answer(player_id, player_name, cleaned, valid=False, duplicate=False, points=0)

        if letter and not normalised.startswith(letter):
            return self._make_answer(player_id, player_name, cleaned, valid=False, duplicate=False, points=0)

        # Only alphanumeric / spaces / hyphens accepted (no pure-symbol spam)
        if not any(c.isalpha() for c in normalised):
            return self._make_answer(player_id, player_name, cleaned, valid=False, duplicate=False, points=0)

        # Per-player dedup within this round
        if normalised in self.player_round_answers.get(player_id, set()):
            return self._make_answer(player_id, player_name, cleaned, valid=True, duplicate=True, points=0)

        # Global dedup across players
        is_duplicate = normalised in self.claimed_answers

        points = 0
        if not is_duplicate:
            points = 1
            self.claimed_answers.add(normalised)
            self.scores[player_id] = self.scores.get(player_id, 0) + points
            self.total_unique_answers += 1

        self.player_round_answers.setdefault(player_id, set()).add(normalised)

        answer = self._make_answer(player_id, player_name, cleaned, valid=True, duplicate=is_duplicate, points=points)
        return answer

    def _make_answer(self, player_id, player_name, text, *, valid, duplicate, points) -> RoundAnswer:
        a = RoundAnswer(player_id, player_name, text, valid, duplicate, points, time.time())
        self.round_answers.append(a)
        return a

    # ── Queries ───────────────────────────────────────────────────

    def get_leaderboard(self) -> list[tuple[str, int]]:
        """Return [(name, score), ...] sorted highest first."""
        return sorted(
            [(self.player_names.get(pid, "Unknown"), score) for pid, score in self.scores.items()],
            key=lambda x: x[1],
            reverse=True,
        )

    def get_round_summary(self) -> dict:
        """Aggregate round answers for results display."""
        # Valid, non-duplicate answers grouped by player
        player_valid: dict[str, list[str]] = {}
        for a in self.round_answers:
            if a.is_valid and not a.is_duplicate and a.points > 0:
                player_valid.setdefault(a.player_name, []).append(a.text)

        duplicates = [a for a in self.round_answers if a.is_duplicate]
        return {"player_valid": player_valid, "duplicates": duplicates}

    # ── Stop ─────────────────────────────────────────────────────

    def stop(self) -> None:
        self.is_active = False
        self.accepting_answers = False
        if self.game_task and not self.game_task.done():
            self.game_task.cancel()
