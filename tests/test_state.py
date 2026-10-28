"""Live request hydration must preserve exact sets and mutable battle mechanics."""
from types import SimpleNamespace

from poke_env.battle import Move, Pokemon, PokemonType

from unaware.state import hydrate_request, hydrate_public_event
from unaware.teams import parse_team


def test_export_spread_and_request_tera_survive_nickname_and_consumed_item():
    mon = Pokemon(9, species="toxtricity")
    mon.moves["boomburst"] = Move("boomburst", gen=9)
    mon.moves["boomburst"]._current_pp = 3
    mon._item = ""  # Throat Spray has already been consumed.
    mon._ability = "punkrock"
    mon._stats = {"hp": 291, "atk": 164, "def": 176, "spa": 359, "spd": 177, "spe": 249}
    mon.boosts["spa"] = 1
    battle = SimpleNamespace(team={"p1: abusa4": mon}, active_pokemon=mon,
        _last_request={"side": {"pokemon": [{"ident": "p1: abusa4", "teraType": "Normal"}]}})
    export = """abusa4 (Toxtricity) @ Throat Spray
Ability: Punk Rock
Tera Type: Normal
EVs: 252 SpA / 4 SpD / 252 Spe
Modest Nature
IVs: 0 Atk
- Boomburst
- Sludge Wave
- Overdrive
- Shift Gear
"""
    hydrate_request(battle, parse_team(export))
    assert mon.tera_type is PokemonType.NORMAL
    assert mon.evs == [0, 0, 0, 252, 4, 252]
    assert mon.ivs == [31, 0, 31, 31, 31, 31]
    assert mon.nature == "modest"
    assert mon.stats["spa"] == 359
    assert mon.boosts["spa"] == 1
    assert mon.item == ""
    assert mon.moves["boomburst"].current_pp == 3


def test_active_request_fills_missing_tera_type_without_export():
    mon = Pokemon(9, species="mimikyu")
    battle = SimpleNamespace(team={"p1: Mimikyu": mon}, active_pokemon=mon,
                             _last_request={"active": [{"canTerastallize": "Ghost"}]})
    hydrate_request(battle)
    assert mon.tera_type is PokemonType.GHOST


def test_hydration_never_changes_observed_terastallization():
    mon = Pokemon(9, species="mimikyu")
    mon._terastallized, mon._terastallized_type = True, PokemonType.FIRE
    battle = SimpleNamespace(team={"p1: Mimikyu": mon}, active_pokemon=mon,
                             _last_request={"active": [{"canTerastallize": "Ghost"}]})
    hydrate_request(battle)
    assert mon.tera_type is PokemonType.FIRE


def test_explicit_flame_orb_source_is_retained_but_burn_alone_does_not_invent_item():
    mon = Pokemon(9, species="hariyama")
    battle = SimpleNamespace(team={}, _opponent_team={"p2: Hariyama": mon})
    hydrate_public_event(battle, ["", "-status", "p2a: Hariyama", "brn"])
    assert mon.item != "flameorb"
    hydrate_public_event(battle, ["", "-status", "p2a: Hariyama", "brn", "[from] item: Flame Orb"])
    assert mon.item == "flameorb"


def test_rocky_helmet_source_belongs_to_holder_not_damage_recipient():
    own, foe = Pokemon(9, species="kingambit"), Pokemon(9, species="skarmory")
    own._item = "blackglasses"
    battle = SimpleNamespace(team={"p1: Kingambit": own}, _opponent_team={"p2: Skarmory": foe})
    hydrate_public_event(battle, ["", "-damage", "p1a: Kingambit", "100/394", "[from] item: Rocky Helmet", "[of] p2a: Skarmory"])
    assert own.item == "blackglasses" and foe.item == "rockyhelmet"
