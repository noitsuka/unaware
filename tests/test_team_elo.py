from types import SimpleNamespace

from unaware.backend import AppService
from unaware.learning import team_fingerprint

TEAM = "Kingambit @ Leftovers\nAbility: Supreme Overlord\n- Sucker Punch\n"


def test_elo_total_counts_only_this_team_in_this_tier():
    mine = team_fingerprint(TEAM, "gen9ou")
    row = lambda team, before, after, fmt="gen9ou", user="Noitsuka": {
        "team_id": team, "format_id": fmt, "username": user, "rating_before": before, "rating_after": after}
    history = [row(mine, 1452, 1470), row(mine, 1470, 1455), row(mine, 1455, 1478),
               row("other", 1300, 1400), row(mine, 1000, 1100, fmt="gen9uu"), row(mine, None, None)]
    backend = SimpleNamespace(config=SimpleNamespace(team=TEAM, format_id="gen9ou", username="noitsuka"), history=history, selected_team="")
    assert AppService._team_elo(backend) == {"delta": 26, "games": 3}


def test_no_rated_games_with_this_team():
    backend = SimpleNamespace(config=SimpleNamespace(team=TEAM, format_id="gen9ou", username="x"), history=[], selected_team="")
    assert AppService._team_elo(backend) == {"delta": None, "games": 0}


def test_overlapping_battles_are_not_counted_twice():
    # Two battles in parallel both start at 1269: the win ends first (1297),
    # the loss later reports 1297 -> 1267. The account went 1269 -> 1267.
    mine = team_fingerprint(TEAM, "gen9ou")
    row = lambda ended, before, after: {"team_id": mine, "format_id": "gen9ou", "username": "x", "ended_at": ended,
                                        "rating_before": before, "rating_after": after}
    history = [row("2026-10-16T12:38:12", 1269, 1267), row("2026-10-16T12:38:09", 1269, 1297)]
    backend = SimpleNamespace(config=SimpleNamespace(team=TEAM, format_id="gen9ou", username="x"), history=history, selected_team="")
    assert AppService._team_elo(backend) == {"delta": -2, "games": 2}
