"""Learning must retain observations without inventing causal explanations."""
import copy
import sqlite3

import pytest
from poke_env.battle import SideCondition

from unaware.engine import Candidate
from unaware.learning import ExperienceStore, MAX_ADJUSTMENT
from unaware.telemetry import BattleTrace, context_key, snapshot
from test_decision_regressions import battle, cinccino, mudsdale


def metadata(number, **updates):
    return {"battle_id": f"learning-v2-{number}", "format_id": "gen9ru", "team_id": "team",
            "result": "loss", "turns": 11, "server_namespace": "local", **updates}


def turn_one_trace():
    own, foe = cinccino(), mudsdale()
    state = battle(own, foe, turn=1)
    state.team = {"p1: Cinccino": own}
    state.opponent_team = {"p2: Mudsdale": foe}
    trace = BattleTrace()
    chosen = Candidate(own.moves["bulletseed"], score=20, label="bulletseed",
                       diagnostics={"expected_dealt": 0.77, "damage_low": 0.70, "damage_high": 0.83,
                                    "has_damage_estimate": 1.0})
    trace.begin(state, [chosen], chosen, snapshot(state))
    trace.observe([["", "move", "p1a: Cinccino", "Bullet Seed", "p2a: Mudsdale"],
                   ["", "-damage", "p2a: Mudsdale", "80/100"],
                   ["", "-damage", "p2a: Mudsdale", "66/100"],
                   ["", "move", "p2a: Mudsdale", "Stealth Rock", "p1a: Cinccino"],
                   ["", "-heal", "p2a: Mudsdale", "72/100", "[from] item: Leftovers"]],
                  "Bot", "p1")
    foe._current_hp = round(foe.max_hp * 0.72)
    foe.boosts["def"] = 2
    state.turn = 2
    trace.close(state)
    return trace.decisions[0]


def test_direct_damage_meter_excludes_leftovers_from_real_turn_one():
    decision = turn_one_trace()
    assert decision["local_outcome"]["direct_dealt"] == pytest.approx(0.34)
    assert decision["local_outcome"]["dealt"] == pytest.approx(0.28, abs=0.002)
    assert decision["observations"]["opponent_moves"] == [
        {"actor": "p2: Mudsdale", "move": "stealthrock"}]


def test_report_retains_boosts_and_move_items_for_reconstructing_errors(tmp_path):
    store = ExperienceStore(tmp_path)
    trace = turn_one_trace()
    trace["after"]["foe_roster"]["p2: Mudsdale"]["password"] = "must-not-leak"
    report = store.record_battle(metadata(1), [trace])
    kept = report["decisions"][0]
    assert kept["after"]["foe_boosts"]["def"] == 2
    assert kept["before"]["foe_roster"]["p2: Mudsdale"]["ability"] == "stamina"
    assert kept["before"]["own_roster"]["p1: Cinccino"]["item"] == "choiceband"
    assert "password" not in kept["after"]["foe_roster"]["p2: Mudsdale"]
    assert kept["before"]["active_id"] == "p1: Cinccino"
    assert report["schema_version"] == 4


def test_v2_context_distinguishes_hazard_layers_ability_and_tera_resource():
    own, foe = cinccino(), mudsdale()
    state = battle(own, foe, side_conditions={SideCondition.SPIKES: 1})
    first = context_key(snapshot(state))
    assert first.startswith("v2:")
    state.side_conditions[SideCondition.SPIKES] = 2
    assert context_key(snapshot(state)) != first
    state.side_conditions[SideCondition.SPIKES] = 1
    foe._ability = "innerfocus"
    assert context_key(snapshot(state)) != first
    foe._ability = "stamina"
    state.can_tera = True
    assert context_key(snapshot(state)) != first


def test_context_uses_hazard_presence_and_relative_screen_age_without_mutating_snapshot():
    own, foe = cinccino(), mudsdale()
    state = battle(own, foe, turn=2,
                   side_conditions={SideCondition.STEALTH_ROCK: 1, SideCondition.STICKY_WEB: 1,
                                    SideCondition.REFLECT: 1})
    raw = snapshot(state)
    before = copy.deepcopy(raw)
    first = context_key(raw)
    assert raw == before
    state.turn = 12
    state.side_conditions = {SideCondition.STEALTH_ROCK: 9, SideCondition.STICKY_WEB: 11,
                             SideCondition.REFLECT: 11}
    assert context_key(snapshot(state)) == first
    state.side_conditions[SideCondition.REFLECT] = 10
    assert context_key(snapshot(state)) != first


def test_observed_move_priors_learn_even_when_critical_blocks_action_rewards(tmp_path):
    store = ExperienceStore(tmp_path)
    trace = turn_one_trace()
    trace["observations"]["critical"] = True
    report = store.record_battle(metadata(1), [trace])
    assert report["learning_updates"] == []
    priors = store.preferences("gen9ru", "mudsdale")["species"]["mudsdale"]
    assert priors["moves"] == {"stealthrock": 1.0}
    assert priors["battles"] == 1
    assert store.preferences("gen9ou")["species"] == {}
    assert report["forecast_updates"] == []


def test_move_spam_in_one_battle_does_not_inflate_prior_confidence(tmp_path):
    store = ExperienceStore(tmp_path)
    store.record_battle(metadata(1), [turn_one_trace() for _ in range(8)])
    prior = store.preferences("gen9ru", "mudsdale")["species"]["mudsdale"]
    assert prior["battles"] == 1
    assert prior["confidence"] < 0.2


def test_calibration_requires_three_battles_and_uses_direct_damage(tmp_path):
    store = ExperienceStore(tmp_path)
    trace = turn_one_trace()
    args = ("gen9ru", "team", trace["context_key"], trace["action_key"])
    store.record_battle(metadata(1), [copy.deepcopy(trace) for _ in range(6)])
    assert store.calibration(*args)["confidence"] == 0.0
    store.record_battle(metadata(2), [trace])
    assert store.calibration(*args)["confidence"] == 0.0
    store.record_battle(metadata(3), [trace])
    measured = store.calibration(*args)
    assert measured["battles"] == 3
    assert measured["confidence"] > 0
    assert measured["bias"] == pytest.approx(-0.43)
    assert measured["outside_range_fraction"] == 1.0
    assert store.calibration("gen9ru", "different-team", *args[2:])["samples"] == 0
    assert abs(store.adjustment(*args)[0]) <= MAX_ADJUSTMENT <= 0.75


def test_disabled_learning_does_not_update_any_memory(tmp_path):
    store = ExperienceStore(tmp_path)
    trace = turn_one_trace()
    report = store.record_battle(metadata(1), [trace], learning_enabled=False)
    assert report["observed_move_updates"] == []
    assert report["forecast_updates"] == []
    assert store.preferences("gen9ru")["species"] == {}
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM forecast_evidence").fetchone()[0] == 0
    assert len(store.history()) == 1


def test_legacy_context_cannot_bias_new_context(tmp_path):
    store = ExperienceStore(tmp_path)
    old = turn_one_trace()
    old["context_key"] = old["context_key"].removeprefix("v2:")
    for number in range(3):
        store.record_battle(metadata(number), [old])
    fresh = turn_one_trace()
    assert store.adjustment("gen9ru", "team", fresh["context_key"], fresh["action_key"])[0] == 0


def test_duplicate_report_callback_cannot_duplicate_move_or_forecast_learning(tmp_path):
    store = ExperienceStore(tmp_path)
    trace = turn_one_trace()
    store.record_battle(metadata(1), [trace])
    store.record_battle(metadata(1), [trace, trace])
    args = ("gen9ru", "team", trace["context_key"], trace["action_key"])
    assert store.calibration(*args)["samples"] == 1
    assert store.preferences("gen9ru", "mudsdale")["species"]["mudsdale"]["battles"] == 1


def test_reset_calibration_keeps_history_and_can_clear_tier_priors(tmp_path):
    store = ExperienceStore(tmp_path)
    trace = turn_one_trace()
    store.record_battle(metadata(1), [trace])
    store.reset_learning(format_id="gen9ru")
    assert store.preferences("gen9ru")["species"] == {}
    args = ("gen9ru", "team", trace["context_key"], trace["action_key"])
    assert store.calibration(*args)["samples"] == 0
    assert len(store.history()) == 1
