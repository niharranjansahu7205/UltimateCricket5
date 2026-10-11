"""
Central place for all premium visual styling: message templates and dividers.
Text-only, no GIFs/media — matches the reference bot's clean card style.
"""

DIVIDER = "🏏━━━━━━━━━━━🏏"

# Local MP4 paths (relative to project root) for shot/wicket animations.
# Drop your own files into the media/ folder using these exact filenames.
MEDIA = {
    1: "media/run_1.mp4",
    2: "media/run_2.mp4",
    3: "media/run_3.mp4",
    4: "media/run_4.mp4",
    5: "media/run_5.mp4",
    6: "media/six.mp4",
    "out": "media/out.mp4",
}


def header(title: str) -> str:
    return f"<b>{title}</b>\n"


def player_tag(name: str) -> str:
    return f"<b>{name}</b>"


def over_line(over_str: str) -> str:
    return f"<u>Over : {over_str}</u>"


def scoreboard_line(batter: str, bowler: str) -> str:
    return f"🏏 Batter : {player_tag(batter)}\n🧤 Bowler : {player_tag(bowler)}"


def runs_result_line(batter: str, runs: int) -> str:
    if runs == 6:
        emoji = "💥🔥"
    elif runs == 4:
        emoji = "⚡"
    else:
        emoji = "🏃"
    return f"{emoji} <i>{batter}</i> smashes it for <b>{runs}</b> run(s)!"


def out_line(batter: str) -> str:
    return f"🎯 <b>OUT!</b> {batter} has been dismissed! 🪦"


def target_line(target: int) -> str:
    return f"🎯 Target set: <b>{target}</b> runs to win!"


def result_card(team_a: str, score_a: int, team_b: str, score_b: int) -> str:
    if score_a > score_b:
        winner, margin = team_a, score_a - score_b
    elif score_b > score_a:
        winner, margin = team_b, score_b - score_a
    else:
        winner, margin = None, 0

    lines = [
        header("🏆 Match Result"),
        f"{player_tag(team_a)} : <b>{score_a}</b> Runs",
        f"{player_tag(team_b)} : <b>{score_b}</b> Runs",
        "",
    ]
    if winner:
        lines.append(f"🎉 <b>{winner}</b> won by <b>{margin}</b> run(s)! 🎉")
    else:
        lines.append("🤝 <b>It's a TIE!</b>")
    return "\n".join(lines)
