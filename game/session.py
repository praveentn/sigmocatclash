"""
GameSession — tracks all state for one active SigmoCatClash game in a channel.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RoundAnswer:
    player_id: int
    player_name: str
    text: str           # original casing from player
    is_valid: bool      # fuzzy-matches a known answer + meets letter/length check
    is_duplicate: bool  # another player already claimed this canonical answer
    points: int
    timestamp: float
    streak_count: int = 0      # player's consecutive valid answers at time of submission
    speed_bonus: bool = False  # first scorer this round (+1 extra pt)
    streak_bonus: bool = False # 3rd+ consecutive valid answer (+1 extra pt)


# ── Fuzzy matching helpers ────────────────────────────────────────────────────

def _levenshtein_distance(s1: str, s2: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    if s1 == s2:
        return 0
    len1, len2 = len(s1), len(s2)
    if len1 == 0:
        return len2
    if len2 == 0:
        return len1

    prev = list(range(len2 + 1))
    for i in range(1, len1 + 1):
        curr = [i] + [0] * len2
        for j in range(1, len2 + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[len2]


def _max_allowed_edits(word_len: int) -> int:
    """Max edit distance allowed based on the length of the submitted word."""
    if word_len <= 3:
        return 0   # exact match required for short words
    elif word_len <= 5:
        return 1   # 1 typo for 4-5 char words
    elif word_len <= 8:
        return 2   # 2 typos for 6-8 char words
    elif word_len <= 12:
        return 3   # 3 typos for 9-12 char words
    else:
        return 4   # 4 typos for very long words


def _find_matching_answer(submitted: str, valid_answers: list) -> Optional[str]:
    """
    Return the canonical answer (lowercased) from valid_answers that best
    fuzzy-matches the submitted text, or None if nothing is within tolerance.
    """
    submitted_lower = submitted.lower()
    max_edits = _max_allowed_edits(len(submitted_lower))

    best_match: Optional[str] = None
    best_dist = max_edits + 1  # sentinel: one more than allowed

    for answer in valid_answers:
        canonical = answer.lower()
        dist = _levenshtein_distance(submitted_lower, canonical)
        if dist <= max_edits and dist < best_dist:
            best_dist = dist
            best_match = canonical
            if best_dist == 0:
                break  # exact match — can't do better

    return best_match


# ── Game session ──────────────────────────────────────────────────────────────

class GameSession:
    def __init__(
        self,
        channel_id: int,
        host_id: int,
        total_rounds: int = 5,
        guild_id: Optional[int] = None,
        pool_key: str = "all",
    ):
        self.channel_id = channel_id
        self.host_id = host_id
        self.total_rounds = total_rounds
        self.guild_id = guild_id
        self.pool_key = pool_key
        self.current_round = 0
        self.is_active = True
        self.accepting_answers = False

        # Persistent across rounds
        self.scores: dict[int, int] = {}              # player_id -> cumulative score
        self.player_names: dict[int, str] = {}        # player_id -> display_name
        self.player_total_answers: dict[int, int] = {} # player_id -> valid answers claimed
        self.total_unique_answers = 0                 # game-wide stat

        # Per-round state — reset in start_round()
        self.current_question: Optional[dict] = None
        self.claimed_answers: set[str] = set()
        self.round_answers: list[RoundAnswer] = []
        self.player_round_answers: dict[int, set[str]] = {}
        self.player_streak: dict[int, int] = {}   # consecutive valid answers per player
        self.first_scorer: Optional[int] = None   # player_id of first scorer this round

        # Async handles
        self.game_task: Optional[asyncio.Task] = None

    # ── Player helpers ────────────────────────────────────────────

    def ensure_player(self, player_id: int, player_name: str) -> None:
        if player_id not in self.scores:
            self.scores[player_id] = 0
            self.player_names[player_id] = player_name
        self.player_names[player_id] = player_name

    # ── Round lifecycle ───────────────────────────────────────────

    def start_round(self, question: dict) -> None:
        self.current_round += 1
        self.current_question = question
        self.claimed_answers = set()
        self.round_answers = []
        self.player_round_answers = {}
        self.player_streak = {}
        self.first_scorer = None
        self.accepting_answers = True

    def end_round(self) -> None:
        self.accepting_answers = False

    # ── Answer submission ─────────────────────────────────────────

    def submit_answer(self, player_id: int, player_name: str, text: str) -> RoundAnswer:
        """
        Validate and record a single answer.

        Scoring:
          base        +1  for any valid unique answer
          speed bonus +1  for the first scorer in a round
          streak bonus+1  for a player's 3rd+ consecutive valid answer
          (invalid answers reset the player's streak; duplicates do not)
        """
        self.ensure_player(player_id, player_name)

        cleaned = text.strip()
        normalised = cleaned.lower()
        letter = (self.current_question.get("letter") or "").strip()
        any_letter = letter == "*"
        valid_answers = self.current_question.get("answers", [])

        def _invalid() -> RoundAnswer:
            self.player_streak[player_id] = 0
            return self._make_answer(player_id, player_name, cleaned,
                                     valid=False, duplicate=False, points=0)

        # ── Validation ────────────────────────────────────────────
        if len(normalised) < 3:
            return _invalid()

        if not any_letter and letter and not normalised.startswith(letter.lower()):
            return _invalid()

        if not any(c.isalpha() for c in normalised):
            return _invalid()

        # Fuzzy-match against known answers; fall back to letter-only if no list
        if valid_answers:
            canonical = _find_matching_answer(normalised, valid_answers)
            if canonical is None:
                return _invalid()
        else:
            canonical = normalised

        # Per-player dedup (doesn't break streak)
        if canonical in self.player_round_answers.get(player_id, set()):
            return self._make_answer(player_id, player_name, cleaned,
                                     valid=True, duplicate=True, points=0)

        # Global dedup
        is_duplicate = canonical in self.claimed_answers

        if not is_duplicate:
            # Streak
            streak_count = self.player_streak.get(player_id, 0) + 1
            self.player_streak[player_id] = streak_count
            streak_bonus = streak_count >= 3

            # Speed — first scorer in this round
            speed_bonus = self.first_scorer is None
            if speed_bonus:
                self.first_scorer = player_id

            points = 1 + (1 if speed_bonus else 0) + (1 if streak_bonus else 0)
            self.claimed_answers.add(canonical)
            self.scores[player_id] = self.scores.get(player_id, 0) + points
            self.player_total_answers[player_id] = self.player_total_answers.get(player_id, 0) + 1
            self.total_unique_answers += 1
        else:
            streak_count = self.player_streak.get(player_id, 0)
            speed_bonus = False
            streak_bonus = False
            points = 0

        self.player_round_answers.setdefault(player_id, set()).add(canonical)
        return self._make_answer(
            player_id, player_name, cleaned,
            valid=True, duplicate=is_duplicate, points=points,
            streak_count=streak_count,
            speed_bonus=speed_bonus,
            streak_bonus=streak_bonus,
        )

    def _make_answer(
        self, player_id, player_name, text, *,
        valid, duplicate, points,
        streak_count=0, speed_bonus=False, streak_bonus=False,
    ) -> RoundAnswer:
        a = RoundAnswer(
            player_id, player_name, text,
            valid, duplicate, points, time.time(),
            streak_count, speed_bonus, streak_bonus,
        )
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
        player_valid: dict[str, list[str]] = {}
        player_points: dict[str, int] = {}
        player_max_streak: dict[str, int] = {}
        player_speed: set[str] = set()

        for a in self.round_answers:
            if a.is_valid and not a.is_duplicate and a.points > 0:
                player_valid.setdefault(a.player_name, []).append(a.text)
                player_points[a.player_name] = player_points.get(a.player_name, 0) + a.points
                if a.streak_count > player_max_streak.get(a.player_name, 0):
                    player_max_streak[a.player_name] = a.streak_count
                if a.speed_bonus:
                    player_speed.add(a.player_name)

        duplicates = [a for a in self.round_answers if a.is_duplicate]
        return {
            "player_valid":      player_valid,
            "player_points":     player_points,
            "player_max_streak": player_max_streak,
            "player_speed":      player_speed,
            "duplicates":        duplicates,
        }

    # ── Stop ─────────────────────────────────────────────────────

    def stop(self) -> None:
        self.is_active = False
        self.accepting_answers = False
        if self.game_task and not self.game_task.done():
            self.game_task.cancel()
