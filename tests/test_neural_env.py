"""Self-play environment for the neural bot (poke-engine Env + OU team states)."""
import random
from pathlib import Path

import pytest

from unaware.neural.teams import team_state

Env = pytest.importorskip("poke_engine.poke_engine").__dict__.get("Env")


@pytest.fixture(scope="module")
def fixed_teams():
    folder = Path(__file__).resolve().parents[1] / "tools" / "samples2026"
    return [(folder / name).read_text(encoding="utf-8-sig") for name in (
        "Dragonite Deoxys-S HO.txt", "3 Attacks NP Gholdengo BO.txt",
    )]


@pytest.mark.skipif(Env is None, reason="poke-engine without the self-play patch")
def test_random_selfplay_steps_preserve_legal_masks(fixed_teams):
    rng = random.Random(0)
    env = Env([team_state(first, second, rng) for first in fixed_teams for second in fixed_teams], seed=3)
    for index in range(len(env)):
        assert env.winner(index) == 0.0
        for _ in range(64):
            m1, m2 = env.masks(index)
            assert any(m1) and any(m2) and len(m1) == len(m2) == 15
            result = env.step(index, rng.choice([i for i, ok in enumerate(m1) if ok]),
                              rng.choice([i for i, ok in enumerate(m2) if ok]))
            assert result in (-1.0, 0.0, 1.0)
            assert env.winner(index) == result
            if result != 0.0:
                break


@pytest.mark.skipif(Env is None, reason="poke-engine without the self-play patch")
@pytest.mark.parametrize("attacker_first,expected", [(True, 1.0), (False, -1.0)])
def test_selfplay_finishes_and_reports_the_winning_side(attacker_first, expected):
    attacker = "Mew\nAbility: Synchronize\n- Psychic\n"
    defender = "Magikarp\nAbility: Swift Swim\n- Splash\n"
    first, second = (attacker, defender) if attacker_first else (defender, attacker)
    env = Env([team_state(first, second, random.Random(0))], seed=3)
    assert env.winner(0) == 0.0
    for _ in range(16):
        m1, m2 = env.masks(0)
        assert m1[0] and m2[0]
        result = env.step(0, 0, 0)
        assert env.winner(0) == result
        if result != 0.0:
            break
    else:
        pytest.fail("The fixed attacking matchup did not finish within 16 turns.")
    assert result == expected


@pytest.mark.skipif(Env is None, reason="poke-engine without the self-play patch")
def test_zero_result_can_mean_the_battle_is_still_running():
    team = "Magikarp\nAbility: Swift Swim\n- Splash\n"
    env = Env([team_state(team, team, random.Random(0))], seed=3)
    for _ in range(3):
        assert env.step(0, 0, 0) == 0.0
        assert env.winner(0) == 0.0
        assert all(mask[0] for mask in env.masks(0))


@pytest.mark.skipif(Env is None or not hasattr(Env, "option_names"), reason="poke-engine without the self-play patch")
def test_option_names_use_foul_play_format(fixed_teams):
    rng = random.Random(1)
    env = Env([team_state(*fixed_teams, rng)], seed=3)
    options = dict(env.option_names(0, 1))
    m1, _ = env.masks(0)
    assert set(options) == {slot for slot, ok in enumerate(m1) if ok}
    assert all(options[s].startswith("switch ") for s in options if 8 <= s <= 13)
    assert all(options[s].endswith("-tera") for s in options if 4 <= s <= 7)
