"""Mechanics regressions prompted by observed OU decision traces.

Only public matchup information is reproduced; accounts and battle IDs are
deliberately absent. Nearby cases distinguish immunity from type resistance.
"""
import pytest
from poke_env.battle import Move, PokemonType, Status

from unaware.engine import DecisionEngine
from unaware.policy import BattlePolicy
from test_decision_regressions import battle, pokemon


def mon(species, moves, *, ability="", item="", hp=1.):
    return pokemon(species, moves, [0, 0, 0, 252, 4, 252], "timid",
                   ability, item, hp)


def status_feature(own, foe, move_id):
    state = battle(own, foe)
    engine = DecisionEngine(allow_tera=False)
    ranked = BattlePolicy().rank(state, engine.rank_actions(state), engine)
    return next(c.features["policy_status"] for c in ranked if c.action.id == move_id)


@pytest.mark.parametrize("species,ability,item", [
    ("ogerpon", "defiant", ""),
    ("mandibuzz", "overcoat", ""),
    ("mew", "synchronize", "safetygoggles"),
])
def test_stun_spore_has_no_status_credit_against_powder_immunity(species, ability, item):
    own = mon("ribombee", ("stunspore", "moonblast"), ability="shielddust")
    foe = mon(species, ("knockoff",), ability=ability, item=item)
    assert status_feature(own, foe, "stunspore") == 0


def test_powder_paralysis_can_affect_ground_types():
    own = mon("ribombee", ("stunspore", "moonblast"), ability="shielddust")
    foe = mon("irontreads", ("earthquake",), ability="quarkdrive")
    assert status_feature(own, foe, "stunspore") > 0


def test_powder_immunity_uses_current_tera_type():
    own = mon("ribombee", ("stunspore", "moonblast"), ability="shielddust")
    foe = mon("ogerpon", ("knockoff",), ability="defiant")
    foe._terastallized_type = PokemonType.FIRE
    foe._terastallized = True
    assert status_feature(own, foe, "stunspore") > 0


@pytest.mark.parametrize("species,ability,item,move_id,own_ability", [
    ("ribombee", "shielddust", "", "bodyslam", "synchronize"),
    ("mew", "synchronize", "covertcloak", "bodyslam", "synchronize"),
    ("mew", "synchronize", "", "bodyslam", "sheerforce"),
    ("gholdengo", "goodasgold", "", "bodyslam", "synchronize"),
    ("chandelure", "flashfire", "", "flamethrower", "synchronize"),
])
def test_blocked_secondary_status_receives_no_credit(species, ability, item, move_id, own_ability):
    own = mon("mew", (move_id, "psychic"), ability=own_ability)
    foe = mon(species, ("psychic",), ability=ability, item=item)
    assert status_feature(own, foe, move_id) == 0


def test_covert_cloak_does_not_block_primary_status():
    own = mon("mew", ("thunderwave", "psychic"), ability="synchronize")
    foe = mon("mew", ("psychic",), ability="synchronize", item="covertcloak")
    assert status_feature(own, foe, "thunderwave") > 0


def test_mold_breaker_bypasses_overcoat_but_not_safety_goggles():
    own = mon("mew", ("stunspore", "psychic"), ability="moldbreaker")
    foe = mon("mandibuzz", ("knockoff",), ability="overcoat")
    assert status_feature(own, foe, "stunspore") > 0
    foe._item = "safetygoggles"
    assert status_feature(own, foe, "stunspore") == 0


def hazard_options(own, foe, *, retention=None):
    state = battle(own, foe)
    state.opponent_team["p2: Bench"] = mon("charizard", ("flamethrower",))
    engine = DecisionEngine(allow_tera=False)
    options = engine.rank_actions(state)
    if retention is not None:
        for candidate in options:
            if candidate.action.id == "stealthrock":
                candidate.diagnostics.update(search_hazard_retention=retention, search_legal=1)
    return BattlePolicy().rank(state, options, engine, {"archetype": "hyper_offense"})


def hazard_feature(options):
    return next(c.features["policy_hazards"] for c in options if c.action.id == "stealthrock")


def test_hazard_credit_uses_retained_layer_instead_of_merely_using_move():
    own = mon("mew", ("stealthrock", "psychic"))
    foe = mon("forretress", ("rapidspin",))
    assert hazard_feature(hazard_options(own, foe, retention=0)) == 0
    assert hazard_feature(hazard_options(own, foe, retention=.4)) == pytest.approx(.2)
    assert hazard_feature(hazard_options(own, foe, retention=1)) == pytest.approx(.5)


def test_simulated_hazard_resolution_does_not_count_full_paralysis_twice():
    own = mon("mew", ("stealthrock", "psychic"))
    own.status = Status.PAR
    foe = mon("forretress", ("rapidspin",))
    assert hazard_feature(hazard_options(own, foe, retention=.4)) == pytest.approx(.2)


def test_revealed_slower_remover_reduces_fallback_hazard_value():
    own = mon("mew", ("stealthrock", "psychic"))
    foe = mon("forretress", ("rapidspin",))
    countered = hazard_feature(hazard_options(own, foe))
    foe.moves["rapidspin"]._current_pp = 0
    exhausted = hazard_feature(hazard_options(own, foe))
    assert 0 < countered < exhausted
    assert exhausted == pytest.approx(.5)


def test_spin_immunity_preserves_fallback_hazard_credit():
    own = mon("palossand", ("stealthrock", "shadowball"))
    foe = mon("forretress", ("rapidspin",))
    assert hazard_feature(hazard_options(own, foe)) == pytest.approx(.5)


def test_faster_remover_cannot_clear_a_layer_set_after_it_moves():
    own = mon("clefable", ("stealthrock", "moonblast"))
    foe = mon("irontreads", ("rapidspin",))
    assert hazard_feature(hazard_options(own, foe)) == pytest.approx(.5)

