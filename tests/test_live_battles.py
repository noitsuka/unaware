"""Live battle list, watching in the browser and concurrency settings."""
import pytest

from unaware.backend import AppService
from unaware.runner import BotConfig


def service(tmp_path):
    return AppService(tmp_path)


def live(tag="battle-gen9ou-1", turn=1):
    return {"battle": tag, "format_id": "gen9ou", "started_at": 0, "turn": turn, "opponent": "Foe",
            "own_team": [], "foe_team": []}


def test_live_battles_are_listed_and_leave_the_list_when_finished(tmp_path):
    app = service(tmp_path)
    app.handle_event({"type": "live_start", "live": live()})
    app.handle_event({"type": "live_update", "live": live(turn=5)})
    assert app.state()["live"][0]["turn"] == 5
    app.handle_event({"type": "live_end", "battle": "battle-gen9ou-1", "result": "win"})
    assert app.state()["live"] == []


def test_watch_opens_the_room_in_the_browser(tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    app = service(tmp_path)
    with pytest.raises(ValueError):
        app.watch({"battle": "../../etc"})
    with pytest.raises(ValueError):
        app.watch({"battle": "battle-gen9ou-42"})  # already finished: no empty room
    app.live["battle-gen9ou-42"] = live("battle-gen9ou-42")
    app.watch({"battle": "battle-gen9ou-42"})
    assert opened == ["https://play.pokemonshowdown.com/battle-gen9ou-42"]


@pytest.mark.parametrize("count, valid", [(1, True), (5, True), (0, False), (6, False)])
def test_concurrency_limits(count, valid):
    config = BotConfig(username="Bot", format_id="gen9randombattle", concurrent_battles=count)
    assert (not any("Concurrent battles" in error for error in config.validate())) is valid


def test_challenges_are_one_at_a_time():
    config = BotConfig(username="Bot", format_id="gen9randombattle", mode="challenge", opponent="Rival", concurrent_battles=3)
    assert any("Challenges" in error for error in config.validate())


def test_cpu_level_changes_while_playing_but_account_does_not(tmp_path):
    app = service(tmp_path)
    app.running = True
    app.update_config({"config": {**app.config.public_dict(), "cpu_level": "low"}})
    assert app.config.cpu_level == "low"
    with pytest.raises(ValueError):
        app.update_config({"config": {**app.config.public_dict(), "format_id": "gen9uu"}})


def test_search_health_counts_full_decisions(tmp_path):
    app = service(tmp_path)
    for mode in ("foulplay", "probabilistic+simulator", "probabilistic+heuristic"):
        app.handle_event({"type": "decision", "evaluation_mode": mode})
    assert app.state()["search_health"] == {"full": 2, "total": 3}


def test_library_lists_and_imports_teams(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "assets").mkdir(exist_ok=True)
    (tmp_path / "assets" / "library.txt").write_text(
        (root / "assets" / "library.txt").read_text(encoding="utf-8"), encoding="utf-8")
    app = service(tmp_path)
    rows = app.library()
    assert len(rows) >= 60 and all(1 <= len(row["species"]) <= 6 for row in rows)
    assert {"gen9ou", "gen9monotype"} <= {row["format_id"] for row in rows}
    app.import_library_team({"name": rows[0]["name"]})
    assert any(team["name"].startswith(rows[0]["name"][:10]) for team in app.teams())


def test_learning_reset_is_blocked_while_playing(tmp_path):
    app = service(tmp_path)
    app.running = True
    with pytest.raises(ValueError):
        app.reset_learning()
    app.running = False
    app.reset_learning()
