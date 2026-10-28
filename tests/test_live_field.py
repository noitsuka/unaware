from types import SimpleNamespace

from poke_env.battle import Battle
from poke_env.battle.side_condition import SideCondition
from poke_env.battle.weather import Weather

from unaware.runner import StrategicPlayer


def test_live_entry_shows_both_sides_of_the_field():
    battle = Battle("battle-gen9ou-1", "me", None, gen=9)
    battle._side_conditions[SideCondition.STEALTH_ROCK] = 3
    battle._opponent_side_conditions[SideCondition.SPIKES] = 2
    battle._opponent_side_conditions[SideCondition.REFLECT] = 5
    battle._weather = {Weather.SNOW: 4}
    bot = SimpleNamespace(live={}, format="gen9ou", traces={}, emit=lambda event: None)
    StrategicPlayer._live_update(bot, battle)
    field = bot.live["battle-gen9ou-1"]["field"]
    assert field == {"own": {"STEALTH_ROCK": 3}, "foe": {"SPIKES": 2, "REFLECT": 5},
                     "weather": ["SNOWSCAPE"], "fields": []}


def test_team_preview_win_chance_is_even_not_96_percent():
    from unaware.neural.winchance import battle_material, material_probability
    battle = Battle("battle-gen9ou-2", "me", None, gen=9)
    battle.player_role = "p1"
    for name in ("Kingambit", "Gholdengo", "Great Tusk", "Dragapult", "Zamazenta", "Gliscor"):
        battle.parse_message(["", "poke", "p2", f"{name}, L100", ""])
    material = battle_material(battle)
    assert material == (6, 6, 6.0, 6.0)
    assert material_probability(*material) == .5

