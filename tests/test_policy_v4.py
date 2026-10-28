"""Tactical behavior and conservative online calibration of the rebuilt policy."""
import sqlite3

import pytest
from poke_env.battle import Field, Move, PokemonType, SideCondition, Status

from unaware.engine import Candidate, DecisionEngine
from unaware.learning import ExperienceStore
from unaware.policy import BattlePolicy, MAX_LEARNED_UTILITY, policy_context
from unaware.strategy import analyze_team
from unaware.teams import TeamMember
from test_decision_regressions import battle, pokemon
from test_learning import battle as record, decision as historical_decision


def mon(species, moves, *, hp=1., ability="", item="", fast=True):
    return pokemon(species, moves, [0, 0, 0, 252, 4, 252] if fast else [252, 0, 252, 0, 4, 0],
                   "timid" if fast else "bold", ability, item, hp)


def ranked(state, style="balance", **kwargs):
    engine = DecisionEngine(allow_tera=False)
    candidates = engine.rank_actions(state)
    return BattlePolicy().rank(state, candidates, engine, {"archetype": style}, **kwargs)


def feature(options, move, key):
    return next(c.features["policy_" + key] for c in options if isinstance(c.action, Move) and c.action.id == move)


def test_scores_rebuilt_instead_of_inheriting_old_ranking():
    own, foe = mon("mew", ("psychic", "nastyplot")), mon("gengar", ("shadowball",), hp=.1)
    state, engine = battle(own, foe), DecisionEngine(allow_tera=False)
    options = engine.rank_actions(state)
    for candidate in options:
        candidate.score = 1e8 if candidate.action.id == "nastyplot" else -1e8
    assert BattlePolicy().rank(state, options, engine)[0].action.id == "psychic"


def test_known_type_immunity_never_outscores_effective_attack_in_last_trade():
    own = mon("ironvaliant", ("closecombat", "moonblast"), hp=.05)
    foe = mon("gholdengo", ("makeitrain",), hp=.35, ability="goodasgold")
    options = ranked(battle(own, foe), "hyper_offense")
    assert options[0].action.id == "moonblast"
    assert feature(options, "closecombat", "damage") == 0


def test_speed_tie_is_half_and_trick_room_does_not_invert_tie():
    own, foe = mon("mew", ("psychic",)), mon("mew", ("psychic",))
    for fields in ({}, {Field.TRICK_ROOM: 1}):
        selected = ranked(battle(own, foe, fields=fields))[0]
        assert selected.diagnostics["policy_first_probability"] == pytest.approx(.5)
        assert selected.diagnostics["policy_speed_tie_probability"] == pytest.approx(1.)


def test_priority_ko_beats_fatal_setup_and_harder_slow_attack():
    own = pokemon("kingambit", ("suckerpunch", "swordsdance", "kowtowcleave"),
                  [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord", hp=.08)
    foe = mon("gengar", ("focusblast",), hp=.15)
    options = ranked(battle(own, foe), "hyper_offense")
    assert options[0].action.id == "suckerpunch"
    assert feature(options, "swordsdance", "setup") == 0


def test_revealed_phazing_does_not_receive_setup_credit():
    own = mon("mew", ("nastyplot", "psychic"))
    foe = mon("skarmory", ("whirlwind", "bravebird"), fast=False)
    reserve = mon("alakazam", ("psychic",))
    options = ranked(battle(own, foe, [reserve]), "hyper_offense")
    assert feature(options, "nastyplot", "setup") == 0


def test_stall_does_not_receive_toxic_credit_against_steel():
    own = mon("blissey", ("toxic", "seismictoss"), fast=False)
    foe = mon("gholdengo", ("shadowball",), ability="goodasgold")
    options = ranked(battle(own, foe), "stall")
    assert feature(options, "toxic", "status") == 0


def test_stall_prefers_reliable_useful_recovery_to_low_damage():
    own = mon("blissey", ("softboiled", "seismictoss"), fast=False, hp=.3)
    foe = mon("mew", ("psychic", "icebeam", "shadowball", "flamethrower"), fast=False)
    options = ranked(battle(own, foe), "stall")
    assert options[0].action.id == "softboiled"
    assert feature(options, "softboiled", "heal") > .45


def test_full_hp_fast_healing_gains_no_fake_credit():
    own, foe = mon("mew", ("recover", "psychic")), mon("blissey", ("seismictoss",), fast=False)
    assert feature(ranked(battle(own, foe)), "recover", "heal") == 0


def test_hazards_use_target_weakness_and_boots():
    own, foe = mon("mew", ("stealthrock", "psychic")), mon("mew", ("psychic",))
    target = mon("charizard", ("flamethrower",))
    state = battle(own, foe)
    state.opponent_team["p2: Charizard"] = target
    options = ranked(state)
    assert feature(options, "stealthrock", "hazards") == pytest.approx(.5)
    target._item = "heavydutyboots"
    assert feature(ranked(state), "stealthrock", "hazards") == 0


def test_hazards_at_layer_cap_have_no_value():
    own, foe = mon("mew", ("spikes", "psychic")), mon("mew", ("psychic",))
    state = battle(own, foe, opponent_side_conditions={SideCondition.SPIKES: 3})
    state.opponent_team["p2: Charizard"] = mon("charizard", ("flamethrower",))
    assert feature(ranked(state), "spikes", "hazards") == 0


def test_spinner_immunity_blocks_removal_credit():
    own = pokemon("greattusk", ("rapidspin", "knockoff"), [252, 0, 252, 0, 4, 0], "impish", "protosynthesis")
    foe = mon("gholdengo", ("shadowball",), ability="goodasgold")
    state = battle(own, foe, side_conditions={SideCondition.SPIKES: 3})
    assert feature(ranked(state), "rapidspin", "removal") == 0


def weather_team():
    return [TeamMember("Pelipper", ability="Drizzle", moves=["Hurricane", "Surf", "Roost", "U-turn"]),
            TeamMember("Barraskewda", ability="Swift Swim", moves=["Liquidation", "Close Combat", "Flip Turn", "Aqua Jet"]),
            TeamMember("Kingdra", ability="Swift Swim", moves=["Surf", "Draco Meteor", "Hurricane", "Hydro Pump"])]


def test_weather_inference_requires_setter_and_multiple_actual_abusers():
    profile = analyze_team(weather_team())
    assert profile["archetype"] == "weather" and profile["weather"] == "rain"
    assert profile["lead_species"] == "pelipper"
    assert analyze_team(weather_team()[:1])["archetype"] != "weather"
    assert analyze_team(weather_team(), "balance")["archetype"] == "balance"


def test_weather_restore_only_when_absent_and_beneficiaries_alive():
    from poke_env.battle import Weather
    own, foe = mon("mew", ("raindance", "surf")), mon("mew", ("psychic",))
    reserve = mon("kingdra", ("surf",), ability="swiftswim")
    state, profile = battle(own, foe, [reserve]), analyze_team(weather_team())
    engine = DecisionEngine(allow_tera=False)
    candidates = BattlePolicy().rank(state, engine.rank_actions(state), engine, profile)
    assert feature(candidates, "raindance", "weather") > 0
    state.weather[Weather.RAINDANCE] = 1
    candidates = BattlePolicy().rank(state, engine.rank_actions(state), engine, profile)
    assert feature(candidates, "raindance", "weather") == 0


def test_style_changes_weights_instead_of_overriding_legal_safety():
    own, foe = mon("mew", ("psychic", "recover"), hp=.5), mon("mew", ("psychic",), fast=False)
    offensive, defensive = ranked(battle(own, foe), "hyper_offense"), ranked(battle(own, foe), "stall")
    scores = lambda rows: {c.action.id: c.score for c in rows}
    ho, stall = scores(offensive), scores(defensive)
    assert stall["recover"] - stall["psychic"] > ho["recover"] - ho["psychic"]


def test_terminal_simulation_and_illegal_reconstruction_are_protected():
    own, foe = mon("mew", ("psychic", "nastyplot")), mon("mew", ("psychic",))
    engine, state = DecisionEngine(allow_tera=False), battle(own, foe)
    options = engine.rank_actions(state)
    for c in options:
        c.diagnostics.update(search_legal=1 if c.action.id == "psychic" else 0,
                             search_worst=9500 if c.action.id == "psychic" else 0)
    result = BattlePolicy().rank(state, options, engine)
    assert result[0].score >= 10000 and result[-1].score == -1e6


def test_corrupt_or_unbounded_learned_weights_cannot_overrule_tactics():
    own, foe = mon("mew", ("psychic", "recover")), mon("mew", ("psychic",))
    state, profile = battle(own, foe), {"archetype": "balance"}
    key = policy_context(state, profile, {})
    options = ranked(state, learned_weights={"contexts": {key: {"damage": 1e100, "heal": float("nan"), "faint_risk": -1e100}}})
    assert all(abs(c.diagnostics["policy_learning"]) <= MAX_LEARNED_UTILITY for c in options)


def policy_decision():
    return historical_decision(
        features={"policy_damage": .7, "policy_incoming": .1, "policy_faint_risk": .1},
        diagnostics={"policy_version": 4, "policy_context": "p4:context", "expected_dealt": .7,
                     "expected_received": .1, "faint_risk": .1, "ko_chance": 0},
        local_outcome={"received": .4, "dealt": .2, "direct_received": .4, "direct_dealt": .2,
                       "own_ko": False, "foe_ko": False, "reliable": True},
    )


def test_learning_requires_three_independent_games_and_preserves_history(tmp_path):
    store = ExperienceStore(tmp_path)
    store.record_battle(record(1), [policy_decision()] * 50)
    assert store.policy_weights("gen9ou", "team-a")["contexts"] == {}
    store.record_battle(record(2), [policy_decision()])
    assert store.policy_weights("gen9ou", "team-a")["contexts"] == {}
    store.record_battle(record(3), [policy_decision()])
    weights = store.policy_weights("gen9ou", "team-a")["contexts"]["p4:context"]
    assert -6 <= weights["damage"] < 0 and -6 <= weights["incoming"] < 0
    assert len(store.history()) == 3
    assert store.policy_weights("gen9uu", "team-a")["contexts"] == {}
    assert store.policy_weights("gen9ou", "other-team")["contexts"] == {}


@pytest.mark.parametrize("flag", ["miss", "critical", "information_revealed", "own_switched", "foe_switched"])
def test_randomness_and_changed_information_do_not_train_policy(tmp_path, flag):
    store = ExperienceStore(tmp_path)
    for number in range(3):
        row = policy_decision()
        row["observations"][flag] = True
        store.record_battle(record(number), [row])
    assert store.policy_weights("gen9ou", "team-a")["contexts"] == {}


def test_learning_duplicate_disabled_reset_and_old_reports_are_safe(tmp_path):
    store = ExperienceStore(tmp_path)
    store.record_battle(record(1), [policy_decision()])
    duplicate = store.record_battle(record(1), [policy_decision()] * 3)
    assert duplicate["duplicate"]
    store.record_battle(record(2), [policy_decision()], learning_enabled=False)
    store.record_battle(record(3), [historical_decision()])
    assert store.policy_weights("gen9ou", "team-a")["contexts"] == {}
    store.reset_learning()
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM policy_evidence").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM battles").fetchone()[0] == 3


def test_complete_statistics_scope_account_tier_server_and_ties(tmp_path):
    store = ExperienceStore(tmp_path)
    for number, result in enumerate(("win", "loss", "tie", "win")):
        store.record_battle(record(number, result=result, server_namespace="showdown"), [])
    store.record_battle(record(10, result="win", username="Other", server_namespace="showdown"), [])
    store.record_battle(record(11, result="win", server_namespace="local:8000"), [])
    stats = store.statistics("gen9ou", "bot", "sim3.psim.us")
    assert stats == {"wins": 2, "losses": 1, "ties": 1, "total": 4, "winrate": 66.67}
    assert store.statistics("gen9uu")["total"] == 0
    store.record_battle(record(12, result="win", server_namespace="localhost:8000"), [])
    store.record_battle(record(13, result="loss", server_namespace="127.0.0.1:8000"), [])
    assert store.statistics(server_namespace="localhost:8000")["total"] == 2
