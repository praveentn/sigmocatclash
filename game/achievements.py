"""
Achievement definitions and checker for SigmoCatClash.
"""

# Achievement catalog — id: {name, emoji, desc}
ACHIEVEMENTS: dict[str, dict] = {
    # Games played milestones
    "first_game":  {"name": "First Steps",       "emoji": "🎮", "desc": "Play your first game"},
    "games_5":     {"name": "Regular",            "emoji": "🎯", "desc": "Play 5 games"},
    "games_10":    {"name": "Veteran",            "emoji": "🏅", "desc": "Play 10 games"},
    "games_25":    {"name": "Dedicated",          "emoji": "🌟", "desc": "Play 25 games"},
    "games_50":    {"name": "Fanatic",            "emoji": "💎", "desc": "Play 50 games"},
    "games_100":   {"name": "Century Club",       "emoji": "🌈", "desc": "Play 100 games"},
    # Wins
    "first_win":   {"name": "First Win",          "emoji": "🥇", "desc": "Win your first game"},
    "wins_5":      {"name": "Champion",           "emoji": "👑", "desc": "Win 5 games"},
    "wins_10":     {"name": "Dominator",          "emoji": "⚔️", "desc": "Win 10 games"},
    "wins_25":     {"name": "Conqueror",          "emoji": "💥", "desc": "Win 25 games"},
    # Total score milestones
    "score_50":    {"name": "Point Collector",    "emoji": "📈", "desc": "Reach 50 total points"},
    "score_150":   {"name": "High Scorer",        "emoji": "🚀", "desc": "Reach 150 total points"},
    "score_300":   {"name": "Point Machine",      "emoji": "⚡", "desc": "Reach 300 total points"},
    "score_500":   {"name": "Elite Scorer",       "emoji": "🔮", "desc": "Reach 500 total points"},
    "score_1000":  {"name": "Legendary Scorer",   "emoji": "🌠", "desc": "Reach 1000 total points"},
    # Best single-game score
    "best_20":     {"name": "Hot Game",           "emoji": "🌡️", "desc": "Score 20+ in one game"},
    "best_30":     {"name": "Blazing",            "emoji": "🔥", "desc": "Score 30+ in one game"},
    "best_40":     {"name": "Unstoppable",        "emoji": "💫", "desc": "Score 40+ in one game"},
    # Daily play streaks
    "streak_3":    {"name": "On a Roll",          "emoji": "🔥", "desc": "Play 3 days in a row"},
    "streak_7":    {"name": "Week Warrior",       "emoji": "🗓️", "desc": "Play 7 days in a row"},
    "streak_14":   {"name": "Fortnight Fighter",  "emoji": "💪", "desc": "Play 14 days in a row"},
    "streak_30":   {"name": "Monthly Legend",     "emoji": "🏆", "desc": "Play 30 days in a row"},
}


def check_new_achievements(entry: dict) -> list[str]:
    """
    Given a (post-update) player entry, return IDs of achievements newly earned.
    Mutates entry["achievements"] in place.
    """
    already: set[str] = set(entry.get("achievements", []))
    earned: list[str] = []

    total_score  = entry.get("total_score", 0)
    games_played = entry.get("games_played", 0)
    best_score   = entry.get("best_score", 0)
    wins         = entry.get("wins", 0)
    c_streak     = entry.get("current_streak", 0)

    checks = [
        ("first_game",  games_played >= 1),
        ("games_5",     games_played >= 5),
        ("games_10",    games_played >= 10),
        ("games_25",    games_played >= 25),
        ("games_50",    games_played >= 50),
        ("games_100",   games_played >= 100),
        ("first_win",   wins >= 1),
        ("wins_5",      wins >= 5),
        ("wins_10",     wins >= 10),
        ("wins_25",     wins >= 25),
        ("score_50",    total_score >= 50),
        ("score_150",   total_score >= 150),
        ("score_300",   total_score >= 300),
        ("score_500",   total_score >= 500),
        ("score_1000",  total_score >= 1000),
        ("best_20",     best_score >= 20),
        ("best_30",     best_score >= 30),
        ("best_40",     best_score >= 40),
        ("streak_3",    c_streak >= 3),
        ("streak_7",    c_streak >= 7),
        ("streak_14",   c_streak >= 14),
        ("streak_30",   c_streak >= 30),
    ]

    for ach_id, condition in checks:
        if condition and ach_id not in already:
            earned.append(ach_id)

    entry["achievements"] = sorted(already | set(earned))
    return earned
