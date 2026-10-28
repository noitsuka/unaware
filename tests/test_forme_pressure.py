"""Observed formes and immunity-aware pressure in the official simulator."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from unaware.engine import Candidate, DecisionEngine
from unaware.state import hydrate_public_event
from unaware.strategy import base_species_id, strategy_action_adjustments
from test_decision_regressions import battle, pokemon
from test_search_strategy import payload, search, worker


def mimikyu(species="mimikyu", hp=1.0):
    return pokemon(species, ("swordsdance", "playrough", "shadowclaw", "shadowsneak"),
                   [0, 252, 0, 0, 4, 252], "jolly", "disguise", "lifeorb", hp)


def dummy():
    return pokemon("mew", ("splash", "recover", "reflect", "lightscreen"),
                   [252, 0, 252, 0, 4, 0], "bold", "synchronize")


def inspect(search, state, actions=None):
    request = payload(search, state)
    request["op"] = "inspect"
    if actions:
        request["testActions"] = actions
    return search._rpc(request)


@pytest.mark.parametrize("message_type,side", [("-formechange", "p1"), ("detailschange", "p2")])
def test_revealed_busted_forme_preserves_mutable_observed_set(message_type, side):
    mon = mimikyu(hp=.6)
    mon.moves["playrough"]._current_pp = 5
    mon.boosts["atk"] = 2
    identifier = side + ": Doll"
    state = SimpleNamespace(gen=9, team={identifier: mon} if side == "p1" else {},
                            opponent_team={identifier: mon} if side == "p2" else {})
    before = copy.deepcopy((mon.stats, mon.evs, mon.ivs, mon.nature, mon.item,
                            mon.ability, mon.current_hp, mon.moves["playrough"].current_pp, mon.boosts))
    hydrate_public_event(state, ["", message_type, side + "a: Doll", "Mimikyu-Busted, L100", "[from] ability: Disguise"])
    assert mon.species == "mimikyubusted"
    assert (mon.stats, mon.evs, mon.ivs, mon.nature, mon.item,
            mon.ability, mon.current_hp, mon.moves["playrough"].current_pp, mon.boosts) == before


def test_base_species_lookup_preserves_plan_without_conflating_similar_names():
    assert base_species_id("Mimikyu-Busted") == "mimikyu"
    assert base_species_id("Palafin-Hero") == "palafin"
    assert base_species_id("Mew") != base_species_id("Mewtwo")
    assert base_species_id("Unknown Species") == "unknownspecies"


def test_busted_forme_keeps_exported_setup_role_and_win_condition():
    own, foe = mimikyu("mimikyubusted", hp=.3), dummy()
    reserve = pokemon("blissey", ("softboiled", "seismictoss"), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state = battle(own, foe, [reserve])
    profile = {"archetype": "hyper_offense", "roles": [{"species": "mimikyu", "roles": ["setup_sweeper", "offensive_attacker"]}],
               "win_conditions": ["mimikyu"]}
    setup = Candidate(own.moves["swordsdance"], score=0, diagnostics={"faint_risk": 0, "expected_received": 0})
    attack = Candidate(own.moves["playrough"], score=0, diagnostics={"faint_risk": .6})
    switch = Candidate(reserve, score=0, diagnostics={"faint_risk": 0})
    results = {row["action_key"]: row for row in strategy_action_adjustments(state, [setup, attack, switch], profile)}
    assert results["move:swordsdance"]["adjustment"] > 0
    assert results["switch:blissey"]["adjustment"] > 0
    assert own.species == "mimikyubusted", "Plan lookup must not rewrite battle mechanics."


def test_worker_leaf_keeps_exported_win_condition_after_public_forme_change(search):
    own, foe = mimikyu(hp=.7), dummy()
    own.boosts["atk"] = 2
    state = battle(own, foe)
    search.strategy_profile = {"archetype": "hyper_offense", "roles": [
        {"species": "mimikyu", "roles": ["setup_sweeper", "cleaner"]}], "win_conditions": ["mimikyu"]}
    intact = inspect(search, state)["evaluation"]
    own._species = "mimikyubusted"
    busted = inspect(search, state)["evaluation"]
    # With only status moves on the opponent, Disguise has no incoming damage
    # to block: losing the public form must not also lose the plan's value.
    assert busted == pytest.approx(intact, abs=.01)


def test_broken_disguise_is_not_restored_after_reconstruction(search):
    own = pokemon("scizor", ("bulletpunch", "swordsdance", "uturn", "roost"),
                   [252, 252, 0, 0, 4, 0], "adamant", "technician")
    foe = mimikyu(hp=.8)
    state = battle(own, foe)
    state.opponent_team = {"p2: Doll": foe}
    hydrate_public_event(state, ["", "-formechange", "p2a: Doll", "Mimikyu-Busted", "[from] ability: Disguise"])
    before = inspect(search, state)
    target = before["sides"][1]["pokemon"][0]
    assert target["species"] == "mimikyubusted"
    after = inspect(search, state, [{"move": "bulletpunch"}, {"move": "swordsdance"}])
    target_after = next(mon for mon in after["sides"][1]["pokemon"] if mon["species"] == "mimikyubusted")
    assert target["hp"] - target_after["hp"] > target["maxhp"] * .2
    assert not any("ability: Disguise" in line and "-activate" in line for line in after["log"])


def test_extreme_speed_has_zero_pressure_into_known_ghost_and_effective_attack_is_chosen(search):
    own = pokemon("dragonite", ("extremespeed", "earthquake", "dragonclaw", "dragondance"),
                   [0, 252, 0, 0, 4, 252], "adamant", "multiscale")
    foe = pokemon("gholdengo", ("recover", "reflect", "lightscreen", "thunderwave"),
                  [252, 0, 252, 0, 4, 0], "bold", "goodasgold")
    state = battle(own, foe)
    state.opponent_can_tera = False
    result = inspect(search, state)
    assert result["pressure"][0]["extremespeed"] == 0
    assert result["pressure"][0]["earthquake"] > 0
    engine = DecisionEngine(allow_tera=False)
    options = [Candidate(own.moves["extremespeed"]), Candidate(own.moves["earthquake"])]
    assert search.rank(state, options, engine)[0].action.id == "earthquake"


def test_scrappy_restores_normal_pressure_and_actual_hits_against_ghost(search):
    own = pokemon("kangaskhan", ("fakeout", "doubleedge", "crunch", "earthquake"),
                   [0, 252, 0, 0, 4, 252], "adamant", "scrappy")
    foe = pokemon("dusknoir", ("willowisp", "painsplit", "shadowpunch", "protect"),
                  [252, 0, 252, 0, 4, 0], "impish", "pressure")
    state = battle(own, foe)
    base = inspect(search, state)
    assert base["pressure"][0]["fakeout"] > 0
    hit = inspect(search, state, [{"move": "fakeout"}, {"move": "willowisp"}])
    assert hit["sides"][1]["pokemon"][0]["hp"] < base["sides"][1]["pokemon"][0]["hp"]
    own._ability = "earlybird"
    assert inspect(search, state)["pressure"][0]["fakeout"] == 0


def test_mold_breaker_restores_ground_pressure_and_actual_hits_against_levitate(search):
    own = pokemon("excadrill", ("earthquake", "ironhead", "rapidspin", "swordsdance"),
                   [0, 252, 0, 0, 4, 252], "jolly", "moldbreaker")
    foe = pokemon("rotomwash", ("protect", "hydropump", "voltswitch", "willowisp"),
                  [252, 0, 252, 0, 4, 0], "bold", "levitate")
    state = battle(own, foe)
    base = inspect(search, state)
    assert base["pressure"][0]["earthquake"] > 0
    hit = inspect(search, state, [{"move": "earthquake"}, {"move": "willowisp"}])
    assert hit["sides"][1]["pokemon"][0]["hp"] < base["sides"][1]["pokemon"][0]["hp"]
    own._ability = "sandrush"
    assert inspect(search, state)["pressure"][0]["earthquake"] == 0


def test_single_hit_pressure_distinguishes_intact_and_already_busted_disguise(search):
    own = pokemon("scizor", ("bulletpunch", "swordsdance", "uturn", "roost"),
                   [252, 252, 0, 0, 4, 0], "adamant", "technician")
    foe = mimikyu()
    state = battle(own, foe)
    intact = inspect(search, state)
    assert intact["pressure"][0]["bulletpunch"] == pytest.approx(foe.max_hp / 8, abs=1)
    foe._species = "mimikyubusted"
    busted = inspect(search, state)
    assert busted["pressure"][0]["bulletpunch"] > intact["pressure"][0]["bulletpunch"] * 2


def test_ice_face_only_blocks_physical_pressure_until_observed_noice_forme(search):
    own = pokemon("mew", ("psychic", "zenheadbutt", "recover", "reflect"),
                   [0, 252, 0, 252, 4, 0], "serious", "synchronize")
    foe = pokemon("eiscue", ("bellydrum", "iciclecrash", "liquidation", "protect"),
                  [252, 0, 252, 0, 4, 0], "impish", "iceface")
    state = battle(own, foe)
    intact = inspect(search, state)
    assert intact["pressure"][0]["zenheadbutt"] == 0
    assert intact["pressure"][0]["psychic"] > 0
    state.opponent_team = {"p2: Cube": foe}
    hydrate_public_event(state, ["", "-formechange", "p2a: Cube", "Eiscue-Noice", "[from] ability: Ice Face"])
    busted = inspect(search, state)
    assert busted["pressure"][0]["zenheadbutt"] > 0


@pytest.mark.parametrize("species,ability,move", [("lanturn", "voltabsorb", "thunderbolt"),
                                                ("gastrodon", "stormdrain", "surf"),
                                                ("orthworm", "eartheater", "earthquake"),
                                                ("azumarill", "sapsipper", "energyball"),
                                                ("dachsbun", "wellbakedbody", "flamethrower")])
def test_absorbing_abilities_have_zero_pressure_and_do_not_mutate_projection_state(search, species, ability, move):
    own = pokemon("mew", (move, "psychic", "recover", "reflect"),
                   [0, 0, 0, 252, 4, 252], "timid", "synchronize")
    foe = pokemon(species, ("tackle", "protect", "rest", "sleeptalk"),
                  [252, 0, 0, 0, 252, 4], "careful", ability, hp=.5)
    state = battle(own, foe)
    result = inspect(search, state)
    assert result["pressure"][0][move] == 0
    assert result["sides"][1]["pokemon"][0]["boosts"] == foe.boosts
    assert result["sides"][1]["pokemon"][0]["hp"] == round(result["sides"][1]["pokemon"][0]["maxhp"] * foe.current_hp_fraction)
    # An observed temporary Mold Breaker (e.g. obtained through Skill Swap)
    # must suppress the absorbing ability in the same move context.
    own._ability = "moldbreaker"
    assert inspect(search, state)["pressure"][0][move] > 0


def test_entei_does_not_choose_extreme_speed_into_intact_disguise_ghost(search):
    own = pokemon("entei", ("extremespeed", "sacredfire", "stoneedge", "trailblaze"),
                   [4, 252, 0, 0, 0, 252], "jolly", "innerfocus", "heavydutyboots", hp=.67)
    foe = mimikyu()
    state = battle(own, foe)
    state.opponent_can_tera = False
    engine = DecisionEngine(allow_tera=False)
    ranked = search.rank(state, engine.rank_actions(state), engine)
    assert ranked[0].action.id != "extremespeed"
    immune = next(candidate for candidate in ranked if candidate.action.id == "extremespeed")
    assert immune.diagnostics["expected_dealt"] == 0


def test_switch_does_not_claim_opponent_life_orb_recoil_as_its_own_move_damage(search):
    own = dummy()
    reserve = pokemon("blissey", ("softboiled", "seismictoss"), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    foe = pokemon("mew", ("thunderbolt", "psychic", "flamethrower", "energyball"),
                  [0, 0, 0, 252, 4, 252], "timid", "synchronize", "lifeorb")
    state = battle(own, foe, [reserve])
    state.opponent_can_tera = False
    engine = DecisionEngine(allow_tera=False)
    switched = search.rank(state, [Candidate(reserve)], engine)[0]
    assert switched.diagnostics["expected_dealt"] == 0
    assert switched.diagnostics["expected_received"] > 0
    resolved = inspect(search, state, [{"switch": 1}, {"move": "thunderbolt"}])
    assert resolved["sides"][1]["pokemon"][0]["hp"] < resolved["sides"][1]["pokemon"][0]["maxhp"]
    assert any("Life Orb" in line for line in resolved["log"])


@pytest.mark.parametrize("species,ability", [("mimikyu", "disguise"), ("eiscue", "iceface")])
def test_mold_breaker_projection_bypasses_intact_hit_shields(search, species, ability):
    own = pokemon("excadrill", ("ironhead", "earthquake", "rapidspin", "swordsdance"),
                   [0, 252, 0, 0, 4, 252], "jolly", "moldbreaker")
    foe = pokemon(species, ("swordsdance", "rest", "protect", "tackle"),
                  [252, 0, 252, 0, 4, 0], "impish", ability)
    state = battle(own, foe)
    bypassed = inspect(search, state)["pressure"][0]["ironhead"]
    own._ability = "sandrush"
    blocked = inspect(search, state)["pressure"][0]["ironhead"]
    assert bypassed > blocked + foe.max_hp * .2


def test_ability_shield_preserves_absorption_against_mold_breaker(search):
    own = pokemon("excadrill", ("earthquake", "ironhead", "rapidspin", "swordsdance"),
                   [0, 252, 0, 0, 4, 252], "jolly", "moldbreaker")
    foe = pokemon("orthworm", ("irondefense", "bodypress", "rest", "sleeptalk"),
                  [252, 0, 252, 0, 4, 0], "impish", "eartheater", "abilityshield")
    assert inspect(search, battle(own, foe))["pressure"][0]["earthquake"] == 0
