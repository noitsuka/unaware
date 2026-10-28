"""Take a sure KO on a weakened target instead of stalling (3.16).

Foul Play's evaluation values a Pokémon at 30 + 100 x HP fraction, so a foe at
8% is worth ~38 points while the hit we take to finish it can cost more. On
14/10 a +2..+6 Gliscor with Facade faced Tornadus at 8% HP for 15 turns and
chose Protect 11 times, Swords Dance 3 times and Earthquake (immune) twice;
Facade KOed in every damage roll. Gliscor fainted and the game was lost.

Across the 44 Foul Play games of 14/10, when one of our attacks had a sure KO,
taking it went 61W-34L and passing on it 65W-72L.

Narrow on purpose (no stacked heuristics, see 3.13): only when Foul Play picked
a move that deals no damage (Protect, setup, recovery, status, hazards), the
foe is below half HP, and a non-Tera attack KOs it with >= 99% by our own
damage estimate, which already accounts for speed, accuracy and our risk of
fainting first. Switches and attacks Foul Play chose are never touched.
"""
from __future__ import annotations

SURE = .99
MAX_FOE_HP = .5
MIN_ACCURACY = .9


def _is_attack(candidate) -> bool:
    action = candidate.action
    return getattr(action, "base_power", 0) > 0 or getattr(action, "damage", 0) not in (0, None, "")


def override(battle, chosen, candidates):
    """The sure-KO candidate to play instead of ``chosen``, or None."""
    from poke_env.battle import Move
    if not isinstance(chosen.action, Move) or _is_attack(chosen):
        return None
    foe = getattr(battle, "opponent_active_pokemon", None)
    if foe is None or foe.fainted or foe.current_hp_fraction >= MAX_FOE_HP:
        return None
    best = None
    for candidate in candidates:
        if candidate.terastallize or not isinstance(candidate.action, Move) or not _is_attack(candidate):
            continue
        info = candidate.diagnostics or {}
        if info.get("ko_chance", 0) < SURE or info.get("accuracy", 0) < MIN_ACCURACY:
            continue
        if best is None or info.get("faint_risk", 1) < (best.diagnostics or {}).get("faint_risk", 1):
            best = candidate
    return best
