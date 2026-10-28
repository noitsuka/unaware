from types import SimpleNamespace

import pytest

from unaware import foulplay_brain


@pytest.fixture()
def search_params():
    if not foulplay_brain.available():
        pytest.skip("Foul Play not available")
    foulplay_brain.configure("me", "gen9ou", 4500, 8)
    from fp.config import FoulPlayConfig
    from fp.modes.standard_battle import StandardBattleMode
    saved = FoulPlayConfig.parallelism
    FoulPlayConfig.parallelism = 8
    yield StandardBattleMode.search_params
    FoulPlayConfig.parallelism = saved


def battle(time_remaining=None, known_moves=4):
    foe = SimpleNamespace(moves=list(range(known_moves)), hp=100)
    return SimpleNamespace(team_preview=False, time_remaining=time_remaining, user=SimpleNamespace(active=None, reserve=[]),
                           opponent=SimpleNamespace(active=foe, reserve=[]))


def test_32_worlds_keep_the_turn_time(search_params):
    mode = SimpleNamespace(bridge_search=(8, 4500), bridge_endgame=False, bridge_worlds=32)
    samples, ms = search_params(mode, battle())
    assert samples == 32 and ms == 4500 // 4  # 4 rounds of 8 lanes, same total time
    samples, ms = search_params(mode, battle(time_remaining=150))
    assert samples == 32 and ms == int(min(25, (150 - 45) / 3) * 1000 / 4)


def test_without_the_setting_foul_play_keeps_its_count(search_params):
    mode = SimpleNamespace(bridge_search=(8, 4500), bridge_endgame=False, bridge_worlds=0)
    assert search_params(mode, battle()) == (8, 4500)
