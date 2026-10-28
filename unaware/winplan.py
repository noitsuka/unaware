"""Win-condition analysis done before any move: how each side can win.

Competitive fundamentals (Smogon guides): every team has win conditions
(setup sweepers, wallbreakers, late-game cleaners) and every game is decided
by keeping your own win condition healthy, removing the opposing checks to it,
and never losing your only answer to the opponent's win condition.

The plan is built at team preview from both rosters, usage sets and scouting,
and refreshed when information is revealed. GamePlan reads it every turn.
"""
from __future__ import annotations


from poke_env.battle import MoveCategory

from .engine import _hp, _opponents
from .strategy import SETUP, base_species_id

THREAT_ITEMS = {"choiceband", "choicespecs", "lifeorb", "boosterenergy"}


def _usage(engine, mon) -> dict:
    try:
        return engine._usage(mon) or {}
    except Exception:
        return {}


def _frequent(values: dict, threshold: float) -> set[str]:
    if not values:
        return set()
    top = max(float(v or 0) for v in values.values()) or 1.
    total = sum(float(v or 0) for v in values.values()) or 1.
    return {name for name, v in values.items() if float(v or 0) / total >= threshold or float(v or 0) >= top * .8}


def _best_damage(engine, attacker, defender, battle) -> float:
    """Expected share of the defender's HP from the attacker's best move."""
    best = 0.
    opponent = attacker in _opponents(battle)
    for move, prior in engine._moves(attacker, opponent=opponent):
        if move.category == MoveCategory.STATUS or (opponent and prior < .2):
            continue
        try:
            best = max(best, engine._damage(attacker, defender, move, battle).expected)
        except Exception:
            continue
    return best


def _threat_level(engine, mon, battle, own_side: bool) -> float:
    """How likely this Pokémon is a win condition (0..1)."""
    usage = _usage(engine, mon)
    moves = set(mon.moves) | ({m.replace(" ", "").replace("-", "").lower() for m in _frequent(usage.get("Moves", {}), .12)}
                              if not own_side else set())
    items = {mon.item} if mon.item and mon.item != "unknown_item" else set()
    if not own_side:
        items |= {i.replace(" ", "").replace("-", "").lower() for i in _frequent(usage.get("Items", {}), .25)}
    setup = bool(moves & SETUP)
    offensive = max(mon.base_stats.get("atk", 0), mon.base_stats.get("spa", 0))
    speed = mon.base_stats.get("spe", 0)
    level = 0.
    level += .45 if setup else 0.
    level += .25 if items & THREAT_ITEMS else 0.
    level += .2 if offensive >= 120 else .1 if offensive >= 100 else 0.
    level += .15 if speed >= 100 else 0.
    if {"extremespeed", "suckerpunch", "aquajet", "bulletpunch", "iceshard", "machpunch", "shadowsneak"} & moves:
        level += .1
    return min(1., level)


class WinPlan:
    """Per-battle plan; cheap to query, rebuilt only when information changes."""

    def __init__(self):
        self.own_wincons: list[dict] = []
        self.foe_threats: list[dict] = []
        self.checks: dict[str, list[str]] = {}
        self.obstacles: dict[str, list[str]] = {}
        self.signature: tuple = ()

    def refresh(self, battle, engine, profile: dict, scouted: dict | None = None) -> "WinPlan":
        own = [mon for mon in getattr(battle, "team", {}).values()]
        foes = _opponents(battle)
        signature = (tuple(sorted(base_species_id(m.species) for m in foes)),
                     sum(len(m.moves) for m in foes), tuple(sorted(str(m.item) for m in foes)),
                     tuple(m.fainted for m in own + foes))
        if signature == self.signature or not own or not foes:
            return self
        self.signature = signature
        conditions = {base_species_id(s) for s in profile.get("win_conditions", [])}
        scouted_species = (scouted or {}).get("species", {})
        # Opposing win conditions: setup/wallbreakers by usage, revealed set and scouting.
        threats = []
        for foe in foes:
            if foe.fainted:
                continue
            level = _threat_level(engine, foe, battle, own_side=False)
            seen = scouted_species.get(base_species_id(foe.species), {})
            if set(seen.get("moves", {})) & SETUP:
                level = min(1., level + .2)
            if level >= .45:
                threats.append({"species": base_species_id(foe.species), "level": round(level, 2)})
        threats.sort(key=lambda row: -row["level"])
        self.foe_threats = threats[:3]
        # Checks: our Pokémon that survive the threat's best hit comfortably and hit back.
        self.checks = {}
        for threat in self.foe_threats:
            foe = next(m for m in foes if base_species_id(m.species) == threat["species"])
            answers = []
            for mon in own:
                if mon.fainted:
                    continue
                taken = _best_damage(engine, foe, mon, battle)
                dealt = _best_damage(engine, mon, foe, battle)
                if taken <= .45 and dealt >= .3 or dealt >= .6 and engine._speed(mon, battle) > engine._speed(foe, battle):
                    answers.append(base_species_id(mon.species))
            self.checks[threat["species"]] = answers
        # Our win conditions and what stands in their way.
        wincons = []
        for mon in own:
            if mon.fainted:
                continue
            level = _threat_level(engine, mon, battle, own_side=True)
            if base_species_id(mon.species) in conditions:
                level = min(1., level + .3)
            if level >= .45:
                wincons.append({"species": base_species_id(mon.species), "level": round(level, 2)})
        wincons.sort(key=lambda row: -row["level"])
        self.own_wincons = wincons[:2]
        self.obstacles = {}
        for wincon in self.own_wincons:
            mon = next(m for m in own if base_species_id(m.species) == wincon["species"])
            walls = []
            for foe in foes:
                if foe.fainted:
                    continue
                dealt = _best_damage(engine, mon, foe, battle)
                taken = _best_damage(engine, foe, mon, battle)
                if dealt < .35 or taken >= .6 and engine._speed(foe, battle) >= engine._speed(mon, battle):
                    walls.append(base_species_id(foe.species))
            self.obstacles[wincon["species"]] = walls
        return self

    def summary(self) -> str:
        if not self.own_wincons and not self.foe_threats:
            return ""
        ours = "; ".join(f"{w['species']} (remove: {', '.join(self.obstacles.get(w['species'], [])) or 'nothing'})"
                         for w in self.own_wincons) or "-"
        theirs = "; ".join(f"{t['species']} (answers: {', '.join(self.checks.get(t['species'], [])) or 'NONE'})"
                           for t in self.foe_threats) or "-"
        return f"Our win conditions: {ours}. Their threats: {theirs}."

    # ---- per-turn questions -------------------------------------------------
    def is_foe_threat(self, species: str) -> float:
        return next((t["level"] for t in self.foe_threats if t["species"] == base_species_id(species)), 0.)

    def is_obstacle(self, species: str) -> bool:
        key = base_species_id(species)
        return any(key in walls for walls in self.obstacles.values())

    def sole_check(self, species: str, battle) -> list[str]:
        """Foe threats (still alive) for which this Pokémon is our last answer."""
        key = base_species_id(species)
        alive_foes = {base_species_id(m.species) for m in _opponents(battle) if not m.fainted}
        alive_own = {base_species_id(m.species) for m in getattr(battle, "team", {}).values() if not m.fainted}
        threats = []
        for threat, answers in self.checks.items():
            living = [a for a in answers if a in alive_own]
            if threat in alive_foes and living == [key]:
                threats.append(threat)
        return threats

    def is_wincon(self, species: str) -> bool:
        return any(w["species"] == base_species_id(species) for w in self.own_wincons)


def plan_value(plan: WinPlan | None, battle, candidate, foe, own, diagnostic: dict, switching: bool, forced: bool) -> tuple[float, list[str]]:
    """Plan-aware adjustment for one candidate, in policy score units."""
    if plan is None or own is None or foe is None:
        return 0., []
    value, reasons = 0., []
    dealt = float(diagnostic.get("expected_dealt") or 0)
    ko = float(diagnostic.get("ko_chance") or 0)
    risk = float(diagnostic.get("faint_risk") or 0)
    if not switching:
        threat = plan.is_foe_threat(foe.species)
        if threat and (dealt > .15 or ko > .2):
            value += threat * (25. * min(1., dealt) + 35. * ko)
            reasons.append(f"Hits the opponent's win condition ({base_species_id(foe.species)}).")
        elif plan.is_obstacle(foe.species) and (dealt > .15 or ko > .2):
            value += 12. * min(1., dealt) + 20. * ko
            reasons.append("Wears down an obstacle to our win condition.")
        last_answer = plan.sole_check(own.species, battle)
        # A vital piece (only answer to 2+ live threats) is protected harder and earlier.
        vital = len(last_answer) >= 2
        if last_answer and risk >= (.35 if vital else .5) and ko < .6:
            value -= 60. if vital else 30.
            reasons.append(f"We are the last answer to {', '.join(last_answer)}: do not trade this piece cheaply.")
        if plan.is_wincon(own.species) and risk >= .5 and ko < .6 and _hp(own) > .4:
            value -= 20.
            reasons.append("Win condition at risk with no gain to make up for it.")
    elif not forced:
        target = candidate.action
        answers = plan.sole_check(target.species, battle)
        vital = len(answers) >= 2
        if answers and risk >= (.35 if vital else .5):
            value -= 50. if vital else 25.
            reasons.append("Brings in our last answer to a threat to take heavy damage.")
    return value, reasons
