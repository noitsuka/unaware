"""Chance outcomes must not collapse an exact speed tie into a certain win."""
import pytest

from unaware.search import SimulatorSearch
from tests.test_engine import battle, engine_for, pokemon


@pytest.mark.parametrize("speed_difference", [0, 1, -1])
def test_lethal_speed_tie_has_balanced_probability(speed_difference):
    own = pokemon("mew", ["tackle"], hp=.01, ability="synchronize")
    foe = pokemon("mew", ["tackle"], hp=.01, ability="synchronize")
    own._stats["spe"] = 280 + speed_difference
    state = battle(own, foe, opponent_can_tera=False)
    engine = engine_for(foe)
    with SimulatorSearch(depth=1, time_budget_ms=200) as worker:
        choice = worker.rank(state, engine.rank_actions(state), engine)[0]
    expected = .5 if speed_difference == 0 else float(speed_difference > 0)
    assert choice.diagnostics["search_win_probability"] == pytest.approx(expected)
    assert choice.diagnostics["search_loss_probability"] == pytest.approx(1 - expected)
    assert choice.diagnostics["ko_chance"] == pytest.approx(expected)
    assert choice.diagnostics["faint_risk"] == pytest.approx(1 - expected)


def test_sacred_fire_burn_is_not_a_guaranteed_defensive_outcome():
    own = pokemon("entei", ["sacredfire"], hp=.7, ability="pressure")
    foe = pokemon("crawdaunt", ["crabhammer"], ability="adaptability")
    own._stats["spe"] = 400
    foe._stats["atk"] = 260
    state = battle(own, foe, opponent_can_tera=False)
    engine = engine_for(foe)
    with SimulatorSearch(depth=1, time_budget_ms=200) as worker:
        choice = worker.rank(state, engine.rank_actions(state), engine)[0]
    # The second scenario must include failure to burn. Scoring only lucky
    # burns was allowing Entei to stay in against a lethal Water attack.
    assert choice.diagnostics["faint_risk"] >= .4
    assert choice.diagnostics["search_received_std"] > 0
