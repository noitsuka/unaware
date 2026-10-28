"""History reset must never erase the bot's experience or user teams."""
import sqlite3

from unaware.learning import ExperienceStore
from test_learning import battle, decision
from test_backend import api, service, TEAM


def evidence(store):
    with sqlite3.connect(store.path) as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("battles", "action_evidence", "policy_evidence", "observed_moves", "progress_evidence")}


def test_reset_preserves_evidence_and_files_but_clears_all_visible_results(tmp_path):
    store = ExperienceStore(tmp_path)
    for index, result in enumerate(("win", "loss", "tie")):
        store.record_battle(battle(index, result=result), [decision()])
    before = evidence(store)
    learned = store.adjustment("gen9ou", "team-a", decision()["context_key"], decision()["action_key"])
    reports = {file: file.read_bytes() for file in (tmp_path / "analyses").iterdir()}
    assert store.clear_history() == 3
    assert store.history() == []
    assert store.statistics() == {"wins": 0, "losses": 0, "ties": 0, "total": 0, "winrate": 0.}
    assert evidence(store) == before
    assert store.adjustment("gen9ou", "team-a", decision()["context_key"], decision()["action_key"]) == learned
    assert all(path.read_bytes() == data for path, data in reports.items())
    assert ExperienceStore(tmp_path).history() == []
    assert store.clear_history() == 0


def test_duplicate_result_or_late_rating_cannot_restore_cleared_match(tmp_path):
    store = ExperienceStore(tmp_path)
    record = battle(1, result="win")
    store.record_battle(record, [decision()])
    store.clear_history()
    store.record_battle(record, [decision()])
    store.update_rating(record["battle_id"], record["server_namespace"], 1500, 1510)
    assert store.history() == [] and store.statistics()["total"] == 0
    store.record_battle(battle(2, result="loss"), [decision()])
    assert [row["battle_id"] for row in store.history()] == [battle(2)["battle_id"]]
    assert store.statistics()["losses"] == 1 and store.statistics()["wins"] == 0


def test_reset_endpoint_auth_persistence_and_active_session_guard(service, api):
    request, _ = api
    service.update_config({"username": "Bot"})
    service.save_team({"name": "Keep", "text": TEAM})
    saved_team = (service.team_dir / "Keep.txt").read_bytes()
    service.experience.record_battle(battle(1, server_namespace="showdown", result="win"), [decision()])
    service.handle_event({"type": "battle_end"})
    assert service.state()["performance"]["wins"] == 1
    assert request("/api/history/reset", "POST", {})[0] == 403
    service.running = True
    assert request("/api/history/reset", "POST", {}, token=True)[0] == 400
    assert len(service.history) == 1
    service.running = False
    assert request("/api/history/reset", "POST", {}, token=True)[0] == 200
    assert service.state()["history"] == []
    assert service.state()["performance"]["total"] == 0
    assert (service.team_dir / "Keep.txt").read_bytes() == saved_team
    service.handle_event({"type": "rating"})
    assert service.state()["history"] == []
