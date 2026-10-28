"""Observed tactical feedback: counters and costly cycles, never random variety.

Evidence is taken from resolved transitions. A repeated move is not itself an
error: damage, poison, useful recovery, retained boosts and hazard removal all
constitute progress. These helpers do not claim an unplayed alternative wins.
"""
from __future__ import annotations

from functools import lru_cache
import hashlib
import json
from typing import Any

from .telemetry import action_key

PHAZING_MOVES = {"whirlwind", "roar", "dragontail", "circlethrow"}


def _n(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


@lru_cache(maxsize=128)
def _setup(move: str) -> bool:
    from poke_env.data import GenData
    data = GenData.from_gen(9).moves.get(move, {})
    boosts = data.get("boosts") or (data.get("self") or {}).get("boosts") or {}
    return data.get("category") == "Status" and data.get("target") == "self" and any(_n(value) > 0 for value in boosts.values())


def _capped_setup(state, move):
    if not _setup(move):
        return False
    from poke_env.data import GenData
    data = GenData.from_gen(9).moves[move]
    boosts = data.get("boosts") or (data.get("self") or {}).get("boosts") or {}
    positive = [stat for stat, value in boosts.items() if _n(value) > 0]
    current = _mon(state).get("boosts") or state.get("own_boosts", {})
    return bool(positive) and all(_n(current.get(stat)) >= 6 for stat in positive)


def _conditions(values):
    return {name: min(3 if name == "SPIKES" else 2, max(1, int(_n(value))))
            if name in {"SPIKES", "TOXIC_SPIKES"} else 1 for name, value in (values or {}).items()}


def _mon(state, side="own"):
    return (state.get(side + "_roster") or {}).get(state.get("active_id" if side == "own" else "foe_id"), {})


def _identity(mon):
    return {key: mon.get(key) for key in ("species", "status", "ability", "item", "types", "tera_type", "terastallized")} | {
        "boosts": {key: _n(value) for key, value in (mon.get("boosts") or {}).items()},
        "effects": sorted((mon.get("effects") or {}).keys())}


def progress_context(state: dict, kind: str, action: str, counter_move: str = "") -> str:
    """Conditional pattern independent of turn number and dwindling HP/PP."""
    data = {"version": 1, "kind": kind, "action": action, "counter": counter_move,
            "own": _identity(_mon(state)), "foe": _identity(_mon(state, "foe")),
            "active": state.get("active"), "foe_active": state.get("foe_active"),
            "own_hazards": _conditions(state.get("own_hazards")),
            "foe_hazards": _conditions(state.get("foe_hazards")),
            "weather": state.get("weather", []), "fields": state.get("fields", []),
            "can_tera": state.get("can_tera", False)}
    return "progress1:" + hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]


def _position(state):
    # Information changes break a cycle. HP and PP are measured separately;
    # absolute hazard start turns cannot distinguish equivalent positions.
    return {"active": state.get("active_id"), "foe": state.get("foe_id"),
            "own": {key: _identity(mon) for key, mon in state.get("own_roster", {}).items()},
            "opponent": {key: _identity(mon) for key, mon in state.get("foe_roster", {}).items()},
            "known": state.get("known_foe_moves", []),
            "own_hazards": _conditions(state.get("own_hazards")), "foe_hazards": _conditions(state.get("foe_hazards")),
            "weather": state.get("weather", []), "fields": state.get("fields", []), "can_tera": state.get("can_tera", False)}


def _hp_total(state, side):
    return sum(_n(value) for value in (state.get("own_hp" if side == "own" else "foe_hp_by_id") or {}).values())


def _costly_cycle(state: dict, previous: list[dict]) -> dict | None:
    if state.get("force_switch") or state.get("finished"):
        return None
    current = _position(state)
    for index in range(len(previous) - 1, max(-1, len(previous) - 13), -1):
        first = previous[index].get("before") or {}
        if state.get("turn", 0) - first.get("turn", 0) < 2 or _position(first) != current:
            continue
        own_loss = _hp_total(first, "own") - _hp_total(state, "own")
        foe_loss = _hp_total(first, "foe") - _hp_total(state, "foe")
        if own_loss < .035 or foe_loss > .025:
            continue
        return {"start_turn": first.get("turn"), "end_turn": state.get("turn"),
                "own_hp_cost": round(own_loss, 5), "foe_hp_progress": round(foe_loss, 5),
                "period": state.get("turn", 0) - first.get("turn", 0),
                "actions": [row.get("action_key", "") for row in previous[index:]],
                "reason": "The same position came back with less of our HP, without lasting damage on the opponent or a field advantage."}
    return None


def transition_evidence(decision: dict, previous: list[dict]) -> list[dict]:
    before, after = decision.get("before") or {}, decision.get("after") or {}
    flags = decision.get("observations") or {}
    if not before or not after or before.get("force_switch") or not flags.get("resolved_action") or flags.get("invalid_choice"):
        return []
    action = str(decision.get("action_key", ""))
    own_move = action.split(":")[1] if action.startswith("move:") else ""
    counter = flags.get("last_move_foe", "")
    evidence = []
    if (_setup(own_move) and flags.get("last_move_own") == own_move and counter in PHAZING_MOVES
            and flags.get("own_switched") and before.get("active_id") != after.get("active_id")
            and _n((after.get("own_hp") or {}).get(before.get("active_id"))) > 0):
        proof = {"turn": decision.get("turn"), "actor": before.get("active"), "foe": before.get("foe_active"),
                 "move": own_move, "counter_move": counter, "returned_to": after.get("active"),
                 "boosts_after": (after.get("own_roster") or {}).get(before.get("active_id"), {}).get("boosts", {}),
                 "own_hp_cost": max(0, round(_hp_total(before, "own") - _hp_total(after, "own"), 5))}
        evidence.append({"kind": "setup_phazed", "pattern_key": progress_context(before, "setup_phazed", action, counter),
                         "action_key": action, "counter_move": counter,
                         "reason": f"{own_move} was undone by {counter}: the Pokémon was forced out and lost its boosts.", "proof": proof})
    if (_setup(own_move) and flags.get("last_move_own") == own_move and counter
            and flags.get("own_fainted") and not any(flags.get(key) for key in ("critical", "miss", "own_switched", "foe_switched"))
            and any(event.get("own") and not event.get("residual") and event.get("move") == counter
                    for event in flags.get("damage_events", []))):
        evidence.append({"kind": "setup_ko", "pattern_key": progress_context(before, "setup_ko", action, counter),
                         "action_key": action, "counter_move": counter,
                         "reason": f"{own_move} went through, but {counter} knocked the user out before the boost paid off.",
                         "proof": {"turn": decision.get("turn"), "actor": before.get("active"), "foe": before.get("foe_active"),
                                   "own_hp_before": before.get("active_hp"), "move": own_move, "counter_move": counter}})
    cycle = _costly_cycle(after, previous + [decision])
    if cycle:
        evidence.append({"kind": "damaging_cycle", "pattern_key": progress_context(before, "damaging_cycle", action),
                         "action_key": action, "counter_move": "", "reason": cycle["reason"], "proof": cycle})
    return evidence


def _counter_available(state, counter):
    foe = _mon(state, "foe")
    if counter in PHAZING_MOVES and state.get("own_alive") is not None and _n(state["own_alive"]) <= 1:
        return False
    effects = {str(name).lower().replace("_", "") for name in (foe.get("effects") or {})}
    if foe.get("status") in {"SLP", "FRZ"}:
        return False
    if counter in {"whirlwind", "roar"} and "taunt" in effects:
        return False
    known = foe.get("moves") or {}
    if counter not in known and counter not in state.get("known_foe_moves", []):
        return False
    return _n(known.get(counter, {}).get("pp"), 1) > 0


def inspect_progress(state: dict, previous: list[dict], memory=()) -> dict:
    recent = previous[-16:]
    evidence = [fact for index, row in enumerate(recent) for fact in transition_evidence(row, recent[:index])]
    patterns = memory.get("patterns", []) if isinstance(memory, dict) else list(memory or ())
    applicable = []
    for fact in evidence + patterns:
        kind, action, counter = fact["kind"], fact["action_key"], fact.get("counter_move", "")
        if fact["pattern_key"] != progress_context(state, kind, action, counter):
            continue
        if kind in {"setup_phazed", "setup_ko"} and not _counter_available(state, counter):
            continue
        if kind == "setup_ko" and _n(state.get("active_hp")) > _n((fact.get("proof") or {}).get("own_hp_before")) + .03:
            continue
        applicable.append(fact)
    return {"evidence": applicable, "cycle": _costly_cycle(state, previous), "version": 1}


def rerank_for_progress(state: dict, candidates: list[Any], history: list[dict], memory=()) -> list[Any]:
    """A bounded feedback term; legality, forced safety and terminal wins dominate."""
    for candidate in candidates:
        candidate.score -= _n(candidate.diagnostics.pop("progress_adjustment", 0))
    if state.get("force_switch") or len(candidates) < 2:
        return candidates
    feedback = inspect_progress(state, history, memory)
    for candidate in candidates:
        if (candidate.diagnostics.get("search_legal") == 0 or candidate.score >= 5000
                or _n(candidate.diagnostics.get("search_worst")) >= 5000):
            continue
        key, penalty, reasons = action_key(candidate), 0.0, []
        move = key.split(":")[1] if key.startswith("move:") else ""
        if _capped_setup(state, move):
            penalty += 8
            reasons.append("The stats this move boosts are already maxed; repeating it gains nothing.")
        matching = [row for row in history[-12:] if row.get("action_key") == key
                    and _identity(_mon(row.get("before") or {})) == _identity(_mon(state))
                    and _identity(_mon(row.get("before") or {}, "foe")) == _identity(_mon(state, "foe"))
                    and (row.get("local_outcome") or {}).get("reliable")
                    and not any((row.get("observations") or {}).get(flag) for flag in ("miss", "critical", "invalid_choice"))]
        ineffective = [row for row in matching if (row.get("observations") or {}).get("own_immune")]
        if ineffective and _n(candidate.diagnostics.get("expected_dealt")) <= .025 and _n(candidate.diagnostics.get("ko_chance")) < .1:
            penalty += 6
            reasons.append("Immunity already seen on this target; the current simulation predicts no useful damage either.")
        from poke_env.data import GenData
        healing = bool((GenData.from_gen(9).moves.get(move) or {}).get("heal"))
        empty_heals = [row for row in matching if (row.get("observations") or {}).get("own_failed")
                       and _n((row.get("before") or {}).get("active_hp")) >= .98
                       and _n((row.get("local_outcome") or {}).get("received")) <= .015]
        if healing and len(empty_heals) >= 2 and _n(state.get("active_hp")) >= .98 and _n(candidate.diagnostics.get("expected_received")) <= .015:
            penalty += 8
            reasons.append("Healing failed repeatedly at full HP; no expected damage justifies repeating it now.")
        negated = [fact for fact in feedback["evidence"] if fact["kind"] in {"setup_phazed", "setup_ko"} and fact["action_key"] == key]
        if negated:
            count = sum(int(fact.get("observations", 1)) for fact in negated)
            penalty += min(24, 6 + 3 * count)
            reasons.append("Setup already undone by a revealed counter that is still available; less future value.")
        cycle = feedback["cycle"]
        if cycle and key in cycle["actions"]:
            # Repeating successful poison stall or attacks does not reach this
            # branch: their lasting progress changes the measured position.
            productive = any(_n(candidate.features.get(name)) > .01 for name in ("heal", "removal", "hazards", "status"))
            productive |= _n(candidate.diagnostics.get("ko_chance")) >= .8
            if not productive:
                penalty += min(12, 2 + 24 * cycle["own_hp_cost"])
                reasons.append("A cycle that costs HP with no lasting progress; future cost taken into account.")
        if penalty:
            candidate.score -= min(30, penalty)
            candidate.diagnostics["progress_adjustment"] = -min(30, penalty)
            candidate.reasons.extend(reason for reason in reasons if reason not in candidate.reasons)
    return sorted(candidates, key=lambda candidate: (-candidate.score, candidate.terastallize, candidate.label))


def feedback_for_search(state: dict, history: list[dict], memory=()) -> dict:
    """Recent observed damage conditions hidden-set hypotheses, not game rules."""
    feedback = inspect_progress(state, history, memory)
    damage = []
    choices = []
    for row in history[-20:]:
        before, flags = row.get("before") or {}, row.get("observations") or {}
        foe = _mon(before, "foe")
        if (before.get("foe_id") == state.get("foe_id") and flags.get("last_move_foe")
                and flags.get("resolved_action") and not flags.get("invalid_choice") and not flags.get("own_switched")
                and foe.get("species") == _mon(state, "foe").get("species")):
            choices.append({"move": flags["last_move_foe"], "target": _mon(before), "attacker": foe,
                            "weather": before.get("weather", []), "fields": before.get("fields", []),
                            "conditions": [before.get("own_hazards", {}), before.get("foe_hazards", {})],
                            "turn": row.get("turn"), "target_hp": before.get("active_hp"), "attacker_hp": before.get("foe_hp")})
    for row in history[-12:]:
        before, flags, outcome = row.get("before") or {}, row.get("observations") or {}, row.get("local_outcome") or {}
        if not outcome.get("reliable") or any(flags.get(key) for key in ("critical", "miss", "own_switched", "foe_switched", "invalid_choice")):
            continue
        # Both actor identities must persist; item/boost/type changes invalidate
        # transporting a damage bound into the current turn.
        if before.get("active_id") != state.get("active_id") or before.get("foe_id") != state.get("foe_id"):
            continue
        if _identity(_mon(before)) != _identity(_mon(state)) or _identity(_mon(before, "foe")) != _identity(_mon(state, "foe")):
            continue
        if (before.get("weather") != state.get("weather") or before.get("fields") != state.get("fields")
                or _conditions(before.get("own_hazards")) != _conditions(state.get("own_hazards"))
                or _conditions(before.get("foe_hazards")) != _conditions(state.get("foe_hazards"))):
            continue
        for side, field, move_field in (("own", "direct_dealt", "last_move_own"), ("foe", "direct_received", "last_move_foe")):
            amount = _n(outcome.get(field))
            if amount > .01 and flags.get(move_field):
                damage.append({"attacker": side, "move": flags[move_field], "amount": amount,
                               "turn": row.get("turn"), "target_hp_before": before.get("foe_hp" if side == "own" else "active_hp"),
                               "target_hp_after": (row.get("after") or {}).get("foe_hp" if side == "own" else "active_hp"),
                               "attacker_hp": before.get("active_hp" if side == "own" else "foe_hp"),
                               "censored": bool(outcome.get("foe_ko" if side == "own" else "own_ko"))})
    return {"version": 1, "observed_damage": damage[-12:], "observed_choices": choices[-8:],
            "counters": feedback["evidence"], "cycle": feedback["cycle"]}
