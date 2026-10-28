import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from unaware.learning import ExperienceStore, MAX_ADJUSTMENT, team_fingerprint


def battle(number=1, **overrides):
    return {"battle_id": f"battle-gen9ou-{number}:session", "battle_tag": f"battle-gen9ou-{number}",
            "server_namespace": "local:8000", "username": "Bot", "format_id": "gen9ou",
            "team_id": "team-a", "opponent": "Rival", "result": "loss", "turns": 15,
            **overrides}


def decision(**overrides):
    return {"turn": 4, "context_key": "pikachu|garchomp|hp=high", "action_key": "move:grassknot:tera0",
            "chosen": "grassknot", "score": 30.0, "features": {"damage": 0.7},
            "diagnostics": {"expected_received": 0.1, "expected_dealt": 0.7, "faint_risk": 0.1},
            "before": {"active": "p1: Pikachu", "active_hp": 1.0, "foe_active": "p2: Garchomp", "foe_hp": 1.0},
            "after": {"active": "p1: Pikachu", "active_hp": 0.9, "foe_active": "p2: Garchomp", "foe_hp": 0.3},
            "observations": {}, "local_outcome": {"received": 0.1, "dealt": 0.7, "own_ko": False, "foe_ko": False, "reliable": True},
            **overrides}


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.store = ExperienceStore(self.base)

    def adjustment(self, team_id="team-a", format_id="gen9ou"):
        return self.store.adjustment(format_id, team_id, decision()["context_key"], decision()["action_key"])[0]

    def evidence_count(self):
        with closing(sqlite3.connect(self.store.path)) as database:
            return database.execute("SELECT COUNT(*) FROM action_evidence").fetchone()[0]

    def test_every_loss_creates_detailed_history_and_portuguese_report(self):
        losing = decision(diagnostics={"faint_risk": 0.85, "expected_received": 0.2, "expected_dealt": 0.7},
                          local_outcome={"received": 1.0, "dealt": 0.0, "own_ko": True, "foe_ko": False, "reliable": True})
        report = self.store.record_battle(battle(), [losing])
        self.assertIn("Loss", report["summary"])
        kinds = {item["kind"] for item in report["findings"]}
        self.assertTrue({"known_risk", "damage_surprise", "damage_shortfall"}.issubset(kinds))
        self.assertTrue(report["hypotheses"])
        self.assertTrue(Path(report["analysis_path"]).exists())
        self.assertEqual(json.loads(Path(report["json_path"]).read_text(encoding="utf-8"))["battle_id"], battle()["battle_id"])
        history = self.store.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["username"], "Bot")
        self.assertEqual(self.store.get_report(history[0]["storage_id"])["summary"], report["summary"])

    def test_losing_match_does_not_punish_locally_successful_action(self):
        for number in range(3):
            self.store.record_battle(battle(number, result="loss"), [decision()])
        self.assertGreater(self.adjustment(), 0)

    def test_threshold_requires_distinct_battles_not_repeated_turns(self):
        self.store.record_battle(battle(1), [decision() for _ in range(10)])
        self.assertEqual(self.adjustment(), 0)
        self.store.record_battle(battle(2), [decision()])
        self.assertEqual(self.adjustment(), 0)
        self.store.record_battle(battle(3), [decision()])
        self.assertGreater(self.adjustment(), 0)
        self.assertLessEqual(abs(self.adjustment()), MAX_ADJUSTMENT)

    def test_tier_and_team_memories_are_isolated(self):
        for number in range(3):
            self.store.record_battle(battle(number), [decision()])
        self.assertEqual(self.adjustment(team_id="different-ivs"), 0)
        self.assertEqual(self.adjustment(format_id="gen9uu"), 0)
        self.assertGreater(self.adjustment(), 0)

    def test_duplicate_callback_cannot_retrain_or_duplicate_history(self):
        first = self.store.record_battle(battle(), [decision()])
        duplicate = self.store.record_battle(battle(), [decision(), decision()])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(self.evidence_count(), 1)
        self.assertEqual(len(self.store.history()), 1)
        self.assertEqual(first["analysis_path"], duplicate["analysis_path"])

    def test_parallel_duplicate_callbacks_are_atomic(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.record_battle(battle(), [decision()]), range(4)))
        self.assertEqual(sum(not item.get("duplicate") for item in results), 1)
        self.assertEqual(self.evidence_count(), 1)

    def test_server_namespace_distinguishes_identical_battle_ids(self):
        self.store.record_battle(battle(server_namespace="one"), [decision()])
        self.store.record_battle(battle(server_namespace="two"), [decision()])
        self.assertEqual(len(self.store.history()), 2)
        self.assertEqual(self.store.get_report(battle()["battle_id"], "two")["server_namespace"], "two")

    def test_disabled_learning_still_records_loss_but_no_memory(self):
        for number in range(3):
            report = self.store.record_battle(battle(number), [decision()], learning_enabled=False)
            self.assertEqual(report["learning_updates"], [])
            self.assertTrue(Path(report["analysis_path"]).exists())
        self.assertEqual(self.evidence_count(), 0)
        self.assertEqual(self.adjustment(), 0)
        self.assertEqual(len(self.store.history()), 3)

    def test_random_events_and_unreliable_transitions_do_not_train(self):
        for number, flag in enumerate(("miss", "critical")):
            report = self.store.record_battle(battle(number), [decision(observations={flag: True})])
            self.assertIn("randomness", {item["kind"] for item in report["findings"]})
        unreliable = decision()
        unreliable["local_outcome"]["reliable"] = False
        self.store.record_battle(battle(3), [unreliable])
        self.assertEqual(self.evidence_count(), 0)

    def test_setup_is_not_punished_just_for_zero_immediate_damage(self):
        setup = decision(chosen="swordsdance", features={"setup": 0.5},
                         local_outcome={"received": 0.25, "dealt": 0, "own_ko": False, "foe_ko": False, "reliable": True})
        report = self.store.record_battle(battle(), [setup])
        self.assertEqual(report["learning_updates"], [])

    def test_forced_switch_never_trains_from_apparent_free_attack(self):
        self.store.record_battle(battle(), [decision(features={"switch": 1}, diagnostics={"forced_switch": 1})])
        self.assertEqual(self.evidence_count(), 0)

    def test_full_hp_heal_with_no_damage_is_noted_and_locally_discouraged(self):
        heal = decision(chosen="recover", features={"heal": 0},
                        local_outcome={"received": 0, "dealt": 0, "own_ko": False, "foe_ko": False, "reliable": True})
        report = self.store.record_battle(battle(), [heal])
        self.assertIn("low_value_heal", {item["kind"] for item in report["findings"]})
        self.assertLess(report["learning_updates"][0]["reward"], 0)

    def test_passive_hp_effects_reduce_confidence(self):
        plain = self.store.record_battle(battle(1), [decision()])
        passive = self.store.record_battle(battle(2), [decision(observations={"residual_or_item": True})])
        self.assertAlmostEqual(passive["learning_updates"][0]["reward"], plain["learning_updates"][0]["reward"] / 2)

    def test_switching_different_active_id_is_not_treated_as_hp_loss(self):
        switched = decision()
        switched.pop("local_outcome")
        switched["after"]["active"] = "p1: Blissey"
        switched["after"]["active_hp"] = 0.1
        report = self.store.record_battle(battle(), [switched])
        self.assertEqual(report["learning_updates"], [])
        self.assertIn("incomplete", {item["kind"] for item in report["findings"]})

    def test_private_config_fields_are_not_serialized(self):
        raw_record = battle(password="do-not-store", team="private-team-text", config={"token": "private-token"},
                            server_namespace="https://username:password@example.com:443/action.php")
        raw_decision = decision(diagnostics={"password": "do-not-store", "token": "private-token", "expected_received": 0.1})
        report = self.store.record_battle(raw_record, [raw_decision])
        serialized = json.dumps(report)
        self.assertNotIn("do-not-store", serialized)
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("private-team-text", serialized)
        self.assertNotIn("username:password", serialized)
        self.assertEqual(report["server_namespace"], "example.com:443")

    def test_late_rating_updates_history_and_files_without_retraining(self):
        original = self.store.record_battle(battle(), [decision()])
        updated = self.store.update_rating(battle()["battle_id"], "local:8000", 1200, 1215)
        self.assertEqual(updated["rating_after"], 1215)
        self.assertEqual(self.evidence_count(), 1)
        self.assertEqual(self.store.history()[0]["rating_before"], 1200)
        self.assertEqual(self.store.history()[0]["rating_after"], 1215)
        self.assertEqual(json.loads(Path(original["json_path"]).read_text(encoding="utf-8"))["rating_after"], 1215)
        self.assertIsNone(self.store.update_rating("missing", "local:8000", 1000, 1100))
        self.assertEqual(len(self.store.history()), 1)

    def test_rolling_memory_is_bounded_and_reset_retains_history(self):
        with patch("unaware.learning.MAX_CONTEXT_SAMPLES", 4):
            for number in range(7):
                self.store.record_battle(battle(number), [decision()])
            self.assertEqual(self.evidence_count(), 4)
            self.assertGreater(self.adjustment(), 0)
        removed = self.store.reset_learning(format_id="gen9ou", team_id="team-a")
        self.assertEqual(removed, 4)
        self.assertEqual(self.adjustment(), 0)
        self.assertEqual(len(self.store.history()), 7)

    def test_team_fingerprint_preserves_stat_changes_and_random_identity(self):
        self.assertEqual(team_fingerprint("Pikachu\r\nIVs: 0 Atk\r\n"), team_fingerprint("Pikachu\nIVs: 0 Atk"))
        self.assertNotEqual(team_fingerprint("Pikachu\nIVs: 0 Atk"), team_fingerprint("Pikachu\nIVs: 31 Atk"))
        self.assertEqual(team_fingerprint("", "gen9randombattle"), "random")


if __name__ == "__main__":
    unittest.main()
