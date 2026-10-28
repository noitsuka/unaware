"""Battle memory for Foul Play: notice a loop without progress and leave it.

Foul Play searches every turn from scratch and never looks back, so it can
repeat a safe-looking move forever. On 14/10 a boosted Gliscor used Protect
11 times next to a Tornadus at 8% HP; Foul Play gave Protect 87% of its visits,
yet its own value estimates were nearly level (Protect .53, Swords Dance .51,
Facade .51). Visits pile onto the safest option; the values show that the
alternatives were almost as good.

A loop: Foul Play is about to play a third non-damaging move in a row (the
same one, or e.g. Protect / Swords Dance alternating, as on 14/10) against the
same opponent, whose HP did not go down since the first of those turns (so
stalling while poison or hazards work is not a loop). Then that move is set
aside and the choice goes to the alternatives whose value is within
VALUE_MARGIN of it, attacks first: the network decides among them in the
hybrid engine, else the best value.
"""
from __future__ import annotations

VALUE_MARGIN = .05
MIN_SHARE = .01
REPEATS = 2  # earlier non-damaging picks that make the next one a loop


def _non_damaging(choice: str, move_json) -> bool:
    if choice.startswith("switch "):
        return False
    move = move_json.get(choice.removesuffix("-tera").removesuffix("-mega"))
    return bool(move) and move.get("category") == "status"


def _snapshot(battle) -> tuple:
    foe = getattr(battle.opponent, "active", None)
    return (getattr(foe, "name", None), float(getattr(foe, "hp", 0) or 0))


def record(memory: list, battle, choice: str) -> None:
    memory.append((battle.turn, choice, *_snapshot(battle)))
    del memory[:-8]


def is_loop(memory: list, battle, choice: str, move_json=None) -> bool:
    if move_json is None:
        from fp.data import all_move_json as move_json
    if not _non_damaging(choice, move_json) or len(memory) < REPEATS:
        return False
    recent = memory[-REPEATS:]
    foe, hp = _snapshot(battle)
    turns = [turn for turn, *_ in recent] + [battle.turn]
    consecutive = all(b - a <= 1 for a, b in zip(turns, turns[1:]))
    return (consecutive and all(_non_damaging(c, move_json) and f == foe for _, c, f, _ in recent)
            and hp >= recent[0][3] - 1e-6)


def alternatives(choice: str, shares: dict, values: dict, move_json=None) -> list[str]:
    """Other options Foul Play visited enough whose value is close to the looped
    one; the attacks among them if there are any."""
    if move_json is None:
        from fp.data import all_move_json as move_json
    floor = values.get(choice, 0.0) - VALUE_MARGIN
    close = [c for c, share in shares.items()
             if c != choice and share >= MIN_SHARE and values.get(c, -1.0) >= floor]
    attacks = [c for c in close if not c.startswith("switch ") and not _non_damaging(c, move_json)]
    return attacks or close
