"""Drop moves that cannot do anything from Foul Play's final choice (3.15).

Foul Play's evaluation does not value PP, so a move that is certain to do
nothing against the current target costs it nothing: it is a free "wait and
see if they switch". In the 211-turn loss to a full stall on 14/10, 37 of our
211 turns were such moves (22 Shadow Balls into Normal types, one of them a
revealed Tera Normal, 11 Toxics into poisoned targets, 4 Stealth Rocks with
rocks already up). Every one burnt PP and gave the stall a free Wish or
Regenerator turn, and the game was lost when our Roost, Soft-Boiled and
Recover ran out.

Only facts known from the battle count (types incl. revealed Tera, status,
hazard layers, abilities that are certain), never guesses. The move is
dropped only when another option has a real share of the search's policy;
if the search has nothing else, its choice stands.
"""
from __future__ import annotations

# A no-op is dropped when some other option has at least this share of the top
# option's policy weight.
MIN_ALTERNATIVE = .33

HAZARD_MAX = {"stealthrock": 1, "stickyweb": 1, "spikes": 3, "toxicspikes": 2}
IGNORES_TYPE_IMMUNITY = {"thousandarrows"}
STATUS_IMMUNE_TYPES = {"tox": {"poison", "steel"}, "psn": {"poison", "steel"},
                       "brn": {"fire"}, "par": {"electric"}}


def _move_id(choice: str) -> str:
    return choice.removesuffix("-tera").removesuffix("-mega")


def _defending_types(pkmn) -> list[str]:
    if getattr(pkmn, "terastallized", False) and pkmn.tera_type and pkmn.tera_type not in ("typeless", "stellar"):
        return [pkmn.tera_type]
    return list(pkmn.types)


def _certain_ability(pkmn, pokedex) -> str:
    """The ability if it was revealed or the species has only one."""
    ability = str(getattr(pkmn, "ability", "") or "")
    if ability and ability not in ("none", "unknown"):
        return ability
    abilities = (pokedex.get(pkmn.name) or {}).get("abilities") or {}
    names = {str(a).lower().replace(" ", "").replace("-", "") for a in abilities.values()}
    return names.pop() if len(names) == 1 else ""


def is_noop(choice: str, battle, move_json=None, pokedex=None, type_modifier=None) -> bool:
    """True when `choice` (a Foul Play move choice) cannot have any effect on the
    opponent's current active Pokémon or side, given what the battle revealed."""
    if choice.startswith("switch "):
        return False
    if move_json is None or pokedex is None or type_modifier is None:
        from fp.battle.helpers import type_effectiveness_modifier
        from fp.data import all_move_json, pokedex as dex
        move_json, pokedex, type_modifier = all_move_json, dex, type_effectiveness_modifier
    move = move_json.get(_move_id(choice))
    user = getattr(battle.user, "active", None)
    foe = getattr(battle.opponent, "active", None)
    if not move or user is None or foe is None or not foe.hp:
        return False
    target = move.get("target")
    if target == "foeSide":
        layers = HAZARD_MAX.get(_move_id(choice))
        return bool(layers) and battle.opponent.side_conditions[_move_id(choice)] >= layers
    if target == "self":
        # Setup whose every boosted stat is already at +6 changes nothing.
        boosts = move.get("boosts") or {}
        current = getattr(user, "boosts", {}) or {}
        return bool(boosts) and all(current.get(stat, 0) >= 6 for stat in boosts)
    if target != "normal":
        return False
    foe_ability = _certain_ability(foe, pokedex)
    user_ability = str(getattr(user, "ability", "") or "")
    if move.get("category") == "status":
        if foe_ability == "goodasgold":
            return True
        status = move.get("status")
        if not status:
            return False
        if foe.status:
            return True
        immune = STATUS_IMMUNE_TYPES.get(status, set())
        if status in ("tox", "psn") and user_ability == "corrosion":
            immune = set()
        return bool(immune & set(_defending_types(foe)))
    if _move_id(choice) in IGNORES_TYPE_IMMUNITY or user_ability in ("scrappy", "mindseye"):
        return False
    if str(getattr(foe, "item", "")) == "ringtarget":
        return False
    if any(v in ("foresight", "odorsleuth", "miracleeye") for v in getattr(foe, "volatile_statuses", [])):
        return False
    try:
        return type_modifier(move["type"], _defending_types(foe)) == 0
    except KeyError:
        return False


def filter_policy(policy: list[tuple[str, float]], battle, **data) -> list[tuple[str, float]]:
    """`policy`: Foul Play's aggregated (choice, weight), best first. Removes the
    no-op moves when an effective option keeps at least MIN_ALTERNATIVE of the
    top weight; otherwise returns it unchanged."""
    if not policy:
        return policy
    top = policy[0][1]
    useful = [p for p in policy if not is_noop(p[0], battle, **data)]
    if len(useful) == len(policy) or not useful or useful[0][1] < top * MIN_ALTERNATIVE:
        return policy
    return useful
