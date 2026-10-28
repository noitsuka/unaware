"""Persistent evidence from resolved loops, with safe legacy reanalysis."""
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from unaware.learning import ExperienceStore
from unaware.progress import inspect_progress, progress_context


KING, TUSK, FOE = "p1: Kingambit", "p1: Great Tusk", "p2: Skarmory"
SD = "move:swordsdance:tera0"


def state(turn=20, active=KING, king_hp=.68, foe_hp=1.0):
    roster = {
        KING: {"species": "kingambit", "hp": king_hp, "status": "", "ability": "supremeoverlord",
               "item": "blackglasses", "types": ["DARK", "STEEL"], "tera_type": "DARK",
               "terastallized": False, "boosts": {"atk": 0}, "effects": {},
               "moves": {"swordsdance": {"pp": 20}, "kowtowcleave": {"pp": 16}}},
        TUSK: {"species": "greattusk", "hp": .67, "status": "", "ability": "protosynthesis",
               "item": "heavydutyboots", "types": ["GROUND", "FIGHTING"], "boosts": {"atk": 0}, "effects": {}},
    }
    foe = {"species": "skarmory", "hp": foe_hp, "status": "", "ability": "sturdy",
           "item": "leftovers", "types": ["STEEL", "FLYING"], "boosts": {"atk": 0}, "effects": {},
           "moves": {"whirlwind": {"pp": 28}, "roost": {"pp": 10}}}
    return {"turn": turn, "active": roster[active]["species"], "active_id": active,
            "foe_active": "skarmory", "foe_id": FOE, "active_hp": roster[active]["hp"], "foe_hp": foe_hp,
            "own_roster": roster, "foe_roster": {FOE: foe}, "own_hp": {KING: king_hp, TUSK: .67},
            "foe_hp_by_id": {FOE: foe_hp}, "own_hp_total": king_hp + .67, "foe_hp_total": foe_hp,
            "known_foe_moves": ["whirlwind", "roost"], "own_hazards": {"STEALTH_ROCK": 8},
            "foe_hazards": {}, "weather": [], "fields": [], "can_tera": False,
            "force_switch": False, "finished": False}


def decision(before, after, action=SD, *, counter="whirlwind", switched=True):
    # The recorded search had features.setup=0. Dex mechanics, rather than
    # that estimate, must identify Swords Dance as preparation.
    flags = {"resolved_action": True, "last_move_own": action.split(":")[1] if action.startswith("move:") else "",
             "last_move_foe": counter, "own_switched": switched,
             "opponent_moves": [{"actor": FOE, "move": counter}]}
    return {"turn": before["turn"], "before": before, "after": after, "action_key": action,
            "chosen": action.split(":")[1], "context_key": "legacy-context", "features": {"setup": 0},
            "diagnostics": {}, "observations": flags,
            "local_outcome": {"reliable": True, "received": max(0, before["own_hp_total"] - after["own_hp_total"]),
                              "dealt": max(0, before["foe_hp_total"] - after["foe_hp_total"]),
                              "own_ko": False, "foe_ko": False}}


def phazed(turn=20, hp=.68):
    return decision(state(turn, king_hp=hp), state(turn + 1, active=TUSK, king_hp=hp - .06))


def encounter(number=1, **overrides):
    return {"battle_id": f"battle-gen9ou-{number}:recording", "battle_tag": f"battle-gen9ou-{number}",
            "server_namespace": "sim3.psim.us", "format_id": "gen9ou", "team_id": "exact-export-fingerprint",
            "username": "Bot", "opponent": "Opponent", "result": "loss", "turns": 33,
            "rating_before": 1310, "rating_after": 1294, "opponent_rating": 1320,
            "replay_path": "saved-replay.txt", **overrides}


class StagnationLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.store = ExperienceStore(self.base)

    def counts(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            return {name: db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in ("battles", "action_evidence", "observed_moves", "forecast_evidence", "progress_evidence")}

    def make_legacy_report(self, rows, *, enabled=True):
        # Simulate a report written by the older analyzer while retaining its
        # original local rewards, observed-move priors and rating metadata.
        with patch("unaware.learning._progress_analysis", return_value=([], [])):
            report = self.store.record_battle(encounter(), rows, learning_enabled=enabled)
        report["schema_version"] = 2
        report.pop("progress_analysis_version")
        report.pop("progress_updates")
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE battles SET report_json=?", (json.dumps(report),))
            db.commit()
        Path(report["json_path"]).write_text(json.dumps(report), encoding="utf-8")
        Path(report["analysis_path"]).write_text("Original recorded analysis\n", encoding="utf-8")
        return report

    def test_one_observed_phaze_is_a_conditional_fact_without_global_move_penalty(self):
        report = self.store.record_battle(encounter(), [phazed()])
        facts = self.store.progress_memory("gen9ou", "exact-export-fingerprint")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["kind"], "setup_phazed")
        self.assertEqual(facts[0]["counter_move"], "whirlwind")
        self.assertEqual(facts[0]["battles"], 1)
        self.assertEqual(facts[0]["confidence"], 1)
        self.assertLess(facts[0]["independent_battle_confidence"], .5)
        self.assertEqual(self.store.adjustment("gen9ou", "exact-export-fingerprint", "unrelated", SD)[0], 0)
        self.assertIn("swordsdance was undone by whirlwind", Path(report["analysis_path"]).read_text(encoding="utf-8"))

    def test_repeated_turns_count_one_independent_match_and_duplicate_callbacks_are_safe(self):
        rows = [phazed(turn, hp) for turn, hp in ((20, .68), (22, .62), (24, .44), (26, .25), (29, .19))]
        self.store.record_battle(encounter(), rows)
        duplicate = self.store.record_battle(encounter(), rows)
        facts = self.store.progress_memory("gen9ou", "exact-export-fingerprint", action_key=SD)
        phazing = next(item for item in facts if item["kind"] == "setup_phazed")
        self.assertEqual((phazing["observations"], phazing["battles"]), (5, 1))
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.counts()["battles"], 1)
        self.store.record_battle(encounter(2), [phazed()])
        phazing = next(item for item in self.store.progress_memory("gen9ou", "exact-export-fingerprint") if item["kind"] == "setup_phazed")
        self.assertEqual((phazing["observations"], phazing["battles"]), (6, 2))

    def test_memory_isolated_by_tier_exact_team_and_conditional_state(self):
        self.store.record_battle(encounter(), [phazed()])
        key = progress_context(state(), "setup_phazed", SD, "whirlwind")
        self.assertEqual(len(self.store.progress_memory("gen9ou", "exact-export-fingerprint", key, SD)), 1)
        self.assertEqual(self.store.progress_memory("gen9uu", "exact-export-fingerprint"), [])
        self.assertEqual(self.store.progress_memory("gen9ou", "different-ivs"), [])
        changed = state()
        changed["foe_roster"][FOE]["species"] = "corviknight"
        self.assertEqual(self.store.progress_memory("gen9ou", "exact-export-fingerprint", progress_context(changed, "setup_phazed", SD, "whirlwind")), [])

    def test_persisted_counter_is_inactive_when_it_cannot_be_used(self):
        self.store.record_battle(encounter(), [phazed()])
        memory = self.store.progress_memory("gen9ou", "exact-export-fingerprint")
        self.assertEqual(len(inspect_progress(state(), [], memory)["evidence"]), 1)
        exhausted = state()
        exhausted["foe_roster"][FOE]["moves"]["whirlwind"]["pp"] = 0
        self.assertEqual(inspect_progress(exhausted, [], memory)["evidence"], [])
        taunted = state()
        taunted["foe_roster"][FOE]["effects"] = {"TAUNT": 1}
        self.assertEqual(inspect_progress(taunted, [], memory)["evidence"], [])

    def test_repeated_setup_with_retained_boosts_does_not_become_phazing_or_cycle_memory(self):
        before, after = state(), state(21, king_hp=.64)
        after["own_roster"][KING]["boosts"]["atk"] = 2
        row = decision(before, after, counter="bravebird", switched=False)
        report = self.store.record_battle(encounter(), [row])
        self.assertEqual(report["progress_updates"], [])
        self.assertEqual(self.store.progress_memory("gen9ou", "exact-export-fingerprint"), [])

    def test_poison_stall_or_hazard_removal_is_progress_despite_own_hp_cost(self):
        initial = state(20)
        poison_end = state(22, king_hp=.54, foe_hp=.75)
        removal_end = state(22, king_hp=.54)
        removal_end["own_hazards"] = {}
        middle = decision(initial, state(21, king_hp=.62), "move:kowtowcleave:tera0", counter="roost", switched=False)
        for number, end in enumerate((poison_end, removal_end), 1):
            end_row = decision(state(21, king_hp=.62), end, "move:kowtowcleave:tera0", counter="roost", switched=False)
            report = self.store.record_battle(encounter(number), [middle, end_row])
            self.assertEqual(report["progress_updates"], [])

    def test_costly_recovery_loop_is_stored_with_measured_proof(self):
        start, middle, end = state(20), state(21, king_hp=.62), state(22, king_hp=.54)
        rows = [decision(start, middle, "move:kowtowcleave:tera0", counter="roost", switched=False),
                decision(middle, end, "move:kowtowcleave:tera0", counter="roost", switched=False)]
        self.store.record_battle(encounter(), rows)
        fact = self.store.progress_memory("gen9ou", "exact-export-fingerprint")[0]
        self.assertEqual(fact["kind"], "damaging_cycle")
        self.assertEqual(fact["action_key"], "move:kowtowcleave:tera0")
        self.assertAlmostEqual(fact["proof"]["own_hp_cost"], .14)
        self.assertEqual(fact["proof"]["foe_hp_progress"], 0)
        self.assertGreaterEqual(fact["proof"]["period"], 2)

    def test_learning_disabled_analyzes_the_problem_without_creating_memory(self):
        report = self.store.record_battle(encounter(), [phazed()], learning_enabled=False)
        self.assertEqual(report["progress_updates"][0]["kind"], "setup_phazed")
        self.assertFalse(report["progress_updates"][0]["learned"])
        self.assertEqual(self.counts()["progress_evidence"], 0)
        self.assertEqual(self.store.progress_memory("gen9ou", "exact-export-fingerprint"), [])

    def test_legacy_reanalysis_backs_up_once_preserves_metadata_and_does_not_retrain(self):
        old = self.make_legacy_report([phazed()])
        before_counts = self.counts()
        old_json = Path(old["json_path"]).read_bytes()
        old_md = Path(old["analysis_path"]).read_bytes()
        updated = self.store.reanalyze_progress(old["storage_id"])
        self.assertEqual(updated["progress_imported"], 1)
        self.assertFalse(updated["progress_duplicate"])
        self.assertEqual(updated["decisions"], old["decisions"])
        self.assertEqual(updated["learning_updates"], old["learning_updates"])
        for field in ("rating_before", "rating_after", "opponent_rating", "username", "opponent", "replay_path", "ended_at", "battle_id"):
            self.assertEqual(updated[field], old[field])
        for name in ("battles", "action_evidence", "observed_moves", "forecast_evidence"):
            self.assertEqual(self.counts()[name], before_counts[name])
        self.assertEqual(Path(old["json_path"] + ".pre-progress-v3.bak").read_bytes(), old_json)
        self.assertEqual(Path(old["analysis_path"] + ".pre-progress-v3.bak").read_bytes(), old_md)
        duplicate = self.store.reanalyze_progress(old["battle_tag"], "sim3.psim.us")
        self.assertTrue(duplicate["progress_duplicate"])
        self.assertEqual(duplicate["progress_imported"], 0)
        self.assertEqual(duplicate["summary"], updated["summary"])
        self.assertEqual(self.counts()["progress_evidence"], 1)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(json.loads(Path(old["json_path"]).read_text(encoding="utf-8"))["progress_analysis_version"], 1)

    def test_reanalysis_of_disabled_learning_keeps_memory_empty(self):
        report = self.make_legacy_report([phazed()], enabled=False)
        updated = self.store.reanalyze_progress(report["battle_id"], "sim3.psim.us")
        self.assertEqual(updated["progress_imported"], 0)
        self.assertFalse(updated["progress_updates"][0]["learned"])
        self.assertEqual(self.counts()["progress_evidence"], 0)

    def test_concurrent_legacy_imports_commit_the_evidence_only_once(self):
        report = self.make_legacy_report([phazed()])
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.reanalyze_progress(report["storage_id"]), range(4)))
        self.assertEqual(sum(item["progress_imported"] for item in results), 1)
        self.assertEqual(sum(not item["progress_duplicate"] for item in results), 1)
        self.assertEqual(self.counts()["progress_evidence"], 1)

    def test_persistent_evidence_has_a_bounded_size_without_removing_history(self):
        with patch("unaware.learning.MAX_TOTAL_SAMPLES", 5):
            for number in range(3):
                self.store.record_battle(encounter(number), [phazed(20), phazed(22), phazed(24)])
        self.assertEqual(self.counts()["progress_evidence"], 5)
        self.assertEqual(self.counts()["battles"], 3)

    def test_reanalysis_missing_or_wrong_server_does_not_touch_data(self):
        report = self.make_legacy_report([phazed()])
        before = self.counts()
        self.assertIsNone(self.store.reanalyze_progress(report["battle_id"], "another-server"))
        self.assertIsNone(self.store.reanalyze_progress("missing"))
        self.assertEqual(self.counts(), before)
        self.assertFalse(Path(report["json_path"] + ".pre-progress-v3.bak").exists())

    def test_copied_database_never_overwrites_analysis_outside_its_base_directory(self):
        report = self.make_legacy_report([phazed()])
        original_bytes = Path(report["json_path"]).read_bytes()
        copied = self.base / "isolated-clone"
        clone = ExperienceStore(copied)
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(clone.path)) as target:
            source.backup(target)
        updated = clone.reanalyze_progress(report["storage_id"])
        self.assertEqual(updated["progress_imported"], 1)
        self.assertEqual(Path(report["json_path"]).read_bytes(), original_bytes)
        self.assertFalse(Path(report["json_path"] + ".pre-progress-v3.bak").exists())
        self.assertEqual(self.counts()["progress_evidence"], 0)

    def test_progress_memory_reset_retains_report_and_ratings(self):
        report = self.store.record_battle(encounter(), [phazed()])
        self.store.reset_learning(format_id="gen9ou", team_id="exact-export-fingerprint")
        self.assertEqual(self.store.progress_memory("gen9ou", "exact-export-fingerprint"), [])
        retained = self.store.get_report(report["storage_id"])
        self.assertEqual(retained["rating_after"], 1294)
        self.assertEqual(retained["progress_updates"], report["progress_updates"])
        self.assertEqual(len(self.store.history()), 1)


if __name__ == "__main__":
    unittest.main()
