"""Two traps Foul Play's search walks into, set aside before its final choice.

Both come from ladder losses of 15/10 replayed through Foul Play turn by turn
(tools/fp_replay.py):

- Destiny Bond. Gengar at 1 HP (Focus Sash) used Destiny Bond; our +1
  Dragonite attacked into it and fainted with it. Replayed, Foul Play gave
  Dragon Dance 2/3 and Earthquake 1/3: a near-tie it settles by drawing, so the
  trade happened by luck. While the opponent's active has Destiny Bond up, a
  damaging move trades our Pokémon for theirs; it is set aside when ours has
  more HP left (a worse trade than doing anything else for one turn).
- Choice lock into a resisted move. A Choice Scarf Darkrai, locked into Dark
  Pulse, stayed three turns against Hoopa-Unbound (resists Dark) and fainted
  to Drain Punch. Foul Play never switched. When our active can only use one
  move (choice lock), that move is resisted or worse by the opponent's active,
  and the opponent is not low, the locked move is set aside for the switches.

Like fp_noop, only facts from the battle count (revealed types incl. Tera,
the volatile, our own item and move list), and an option is only removed when
something else is left.
"""
from __future__ import annotations

LOW_FOE_HP = .5  # under this the locked hit may still finish the opponent


def _move_id(choice: str) -> str:
    return choice.removesuffix("-tera").removesuffix("-mega")


def _damaging(choice: str, move_json) -> bool:
    if choice.startswith("switch "):
        return False
    move = move_json.get(_move_id(choice)) or {}
    return move.get("category") in ("physical", "special")


def _fraction(pkmn) -> float:
    return pkmn.hp / pkmn.max_hp if getattr(pkmn, "max_hp", 0) else 0.


def _defending_types(pkmn) -> list[str]:
    if getattr(pkmn, "terastallized", False) and pkmn.tera_type and pkmn.tera_type not in ("typeless", "stellar"):
        return [pkmn.tera_type]
    return list(pkmn.types)


def destiny_bond_trade(choice: str, battle, move_json=None) -> bool:
    """A damaging move into an active Destiny Bond, with our Pokémon worth more."""
    if move_json is None:
        from fp.data import all_move_json as move_json
    foe, user = getattr(battle.opponent, "active", None), getattr(battle.user, "active", None)
    if foe is None or user is None or "destinybond" not in (foe.volatile_statuses or []):
        return False
    return _damaging(choice, move_json) and _fraction(user) > _fraction(foe)


def locked_into_resisted(choice: str, battle, move_json=None, type_modifier=None) -> bool:
    """The only usable move (choice lock) is resisted by a healthy opponent."""
    if choice.startswith("switch "):
        return False
    if move_json is None or type_modifier is None:
        from fp.battle.helpers import type_effectiveness_modifier
        from fp.data import all_move_json
        move_json, type_modifier = all_move_json, type_effectiveness_modifier
    foe, user = getattr(battle.opponent, "active", None), getattr(battle.user, "active", None)
    if foe is None or user is None or not str(getattr(user, "item", "")).startswith("choice"):
        return False
    usable = [m for m in user.moves if not getattr(m, "disabled", False) and getattr(m, "current_pp", 1) > 0]
    if len(usable) != 1 or _move_id(choice) != usable[0].name or _fraction(foe) < LOW_FOE_HP:
        return False
    move = move_json.get(usable[0].name) or {}
    if move.get("category") not in ("physical", "special"):
        return False
    try:
        return type_modifier(move["type"], _defending_types(foe)) <= .5
    except KeyError:
        return False


def apply(policy: list[tuple[str, float]], battle, **data) -> list[tuple[str, float]]:
    """`policy`: (choice, weight) best first. Removes the trapped options when
    anything else is left; for the choice lock only switches are left, so the
    best of them (by weight) is played."""
    trapped = [c for c, _ in policy
               if destiny_bond_trade(c, battle, data.get("move_json")) or locked_into_resisted(c, battle, **data)]
    rest = [p for p in policy if p[0] not in trapped]
    return rest if rest and trapped else policy
