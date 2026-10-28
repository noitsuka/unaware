"""Starting states for self-play: two OU teams -> a poke-engine state string."""
from __future__ import annotations

import random
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEAM_DIRS = (ROOT / "tools" / "samples2026", ROOT / "teams")
STATS = ("hp", "atk", "def", "spa", "spd", "spe")


def load_teams() -> dict[str, str]:
    """Every validated team text we have (Smogon samples + the user's teams)."""
    teams = {}
    for folder in TEAM_DIRS:
        for path in sorted(folder.glob("*.txt")):
            text = path.read_text(encoding="utf-8-sig").replace("\r", "").strip()
            if text.count("\n- ") >= 4:
                teams[path.stem] = text + "\n"
    return teams


def _configure() -> None:
    from unaware import foulplay_brain
    foulplay_brain.configure("selfplay", "gen9ou", 1000, 1)


def _pokemon(entry: dict):
    from fp.battle.state import Pokemon
    evs = tuple(int(entry["evs"].get(stat) or 0) for stat in STATS)
    mon = Pokemon(entry["species"], 100, nature=entry.get("nature") or "serious", evs=evs)
    mon.set_spread(entry.get("nature") or "serious", list(evs))
    mon.hp = mon.max_hp
    mon.ability = entry.get("ability") or mon.ability
    mon.item = entry.get("item") or "none"
    mon.tera_type = entry.get("tera_type") or None
    for move in entry["moves"]:
        mon.add_move(move)
    return mon


def team_state(team_one: str, team_two: str, rng: random.Random | None = None) -> str:
    """Initial state with random leads (both teams fully known to the engine;
    the network's observation masks what a player could not have seen)."""
    _configure()
    from fp.battle.state import Battle
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state
    from fp.teams.team_converter import export_to_dict
    rng = rng or random.Random()
    battle = Battle("selfplay")
    for side, text in ((battle.user, team_one), (battle.opponent, team_two)):
        mons = [_pokemon(entry) for entry in export_to_dict(text)]
        rng.shuffle(mons)
        side.active, side.reserve = mons[0], mons[1:]
    return battle_to_poke_engine_state(battle).to_string()
