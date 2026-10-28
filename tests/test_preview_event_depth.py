"""Regression: failed estimates must not leak Showdown's event depth.

The leak made every preview and most searches fail with "Stack overflow"
(weather teams in particular), dropping the bot to its emergency evaluator.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest
from poke_env.battle import Pokemon
from poke_env.teambuilder import ConstantTeambuilder

from unaware.engine import DecisionEngine
from unaware.search import SimulatorSearch

ROOT = Path(__file__).resolve().parents[1]
RAIN = (ROOT / "tools/library_seed.txt").read_text(encoding="utf-8").split("=== Rain | weather")[1].split("=== ")[0]


@pytest.mark.parametrize("workers", [1, 3])
def test_rain_preview_completes(workers):
    team = [Pokemon(9, teambuilder=member) for member in ConstantTeambuilder(RAIN).team]
    foes = [Pokemon(9, species=name) for name in ("greattusk", "gliscor", "kingambit", "dragapult", "slowkinggalar", "zamazenta")]
    battle = SimpleNamespace(team={f"p1: {m.species}": m for m in team}, teampreview_team=team,
                             teampreview_opponent_team=foes, opponent_team={}, gen=9)
    with SimulatorSearch(workers=workers) as search:
        order = search.preview_order(battle, DecisionEngine())
    assert sorted(order) == [1, 2, 3, 4, 5, 6]
