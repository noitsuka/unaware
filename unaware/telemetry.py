"""Observed transitions for post-game analysis; never fabricates hidden state."""
from __future__ import annotations

import hashlib
import html
import json
import re

from poke_env.battle import Pokemon
from poke_env.data import to_id_str


def _hp(mon):
    return round(float(mon.current_hp_fraction), 5) if mon else 0.0


def _status(mon):
    return mon.status.name if mon and mon.status else ""


def _identity(value: str) -> str:
    """Protocol active slot p2a and roster p2 refer to the same individual."""
    return value[:2] + ": " + value.split(":", 1)[1].strip() if ":" in value else value


def _condition_hp(value: str) -> float | None:
    if "fnt" in value:
        return 0.0
    try:
        numerator, denominator = value.split(" ", 1)[0].split("/", 1)
        return max(0.0, min(1.0, float(numerator) / float(denominator)))
    except (ValueError, ZeroDivisionError):
        return None


def _pokemon_state(mon: Pokemon) -> dict:
    """Only known battle state; no login details or invented opponent spreads."""
    return {
        "species": mon.species, "hp": _hp(mon), "status": _status(mon),
        "ability": mon.ability or "", "item": mon.item or "", "level": mon.level,
        "stats": dict(mon.stats), "boosts": dict(mon.boosts),
        "types": [kind.name for kind in mon.types],
        "tera_type": mon.tera_type.name if mon.tera_type else "",
        "terastallized": mon.is_terastallized,
        "moves": {key: {"pp": move.current_pp, "max_pp": move.max_pp}
                  for key, move in mon.moves.items()},
        "effects": {key.name: value for key, value in mon.effects.items()},
    }


def snapshot(battle) -> dict:
    own = dict(battle.team)
    # Observed opponents only; preview species are not fictitious HP observations.
    foes = dict(getattr(battle, "_opponent_team", battle.opponent_team))
    active, foe = battle.active_pokemon, battle.opponent_active_pokemon
    own_key = next((key for key, mon in own.items() if mon is active), "")
    foe_key = next((key for key, mon in foes.items() if mon is foe), "")
    own_hp = {key: _hp(mon) for key, mon in own.items()}
    foe_hp = {key: _hp(mon) for key, mon in foes.items()}
    return {
        "schema_version": 2,
        "turn": battle.turn, "active": active.species if active else "",
        "active_id": own_key, "active_hp": _hp(active), "own_hp": own_hp,
        "own_hp_total": sum(own_hp.values()), "own_alive": sum(value > 0 for value in own_hp.values()),
        "foe_active": foe.species if foe else "", "foe_id": foe_key,
        "foe_hp": _hp(foe), "foe_hp_by_id": foe_hp,
        "foe_hp_total": sum(foe_hp.values()), "foe_alive": sum(value > 0 for value in foe_hp.values()),
        "own_status": _status(active), "foe_status": _status(foe),
        "own_boosts": dict(active.boosts) if active else {},
        "foe_boosts": dict(foe.boosts) if foe else {},
        "known_foe_moves": sorted(foe.moves) if foe else [],
        "own_hazards": {k.name: v for k, v in battle.side_conditions.items()},
        "foe_hazards": {k.name: v for k, v in battle.opponent_side_conditions.items()},
        "weather": sorted(k.name for k in battle.weather),
        "fields": sorted(k.name for k in battle.fields),
        "force_switch": bool(battle.force_switch),
        "finished": bool(battle.finished),
        "own_roster": {key: _pokemon_state(mon) for key, mon in own.items()},
        "foe_roster": {key: _pokemon_state(mon) for key, mon in foes.items()},
        "preview_foes": [mon.species for mon in getattr(battle, "teampreview_opponent_team", [])],
        "can_tera": bool(getattr(battle, "can_tera", False)),
    }


def context_key(state: dict) -> str:
    def bucket(value):
        return min(3, max(0, int(float(value) * 4)))
    def conditions(values):
        # poke-env stores layers for Spikes, but an absolute start turn for
        # other conditions. Absolute turns fragment otherwise identical games.
        timeless = {"STEALTH_ROCK", "STICKY_WEB", "G_MAX_STEELSURGE"}
        stackable = {"SPIKES": 3, "TOXIC_SPIKES": 2}
        normalized = {}
        for name, value in values.items():
            if name in timeless:
                normalized[name] = 1
            elif name in stackable:
                normalized[name] = min(stackable[name], max(1, int(value)))
            else:
                # Preserve relative age for screens/Tailwind. Their source's
                # item may be hidden, so do not invent an exact expiration.
                normalized[name] = {"age": min(8, max(0, state["turn"] - int(value)))}
        return normalized
    # Coarse states permit repeated evidence; tactical scoring remains primary.
    own = state.get("own_roster", {}).get(state.get("active_id"), {})
    foe = state.get("foe_roster", {}).get(state.get("foe_id"), {})
    context = {
        "version": 2, "self": state["active"], "foe": state["foe_active"],
        "hp": bucket(state["active_hp"]), "foe_hp": bucket(state["foe_hp"]),
        "status": state["own_status"], "foe_status": state["foe_status"],
        "boosts": state["own_boosts"], "foe_boosts": state["foe_boosts"],
        "hazards": conditions(state["own_hazards"]), "foe_hazards": conditions(state.get("foe_hazards", {})), "weather": state["weather"],
        "fields": state["fields"], "forced": state["force_switch"],
        "own_ability": own.get("ability", ""), "foe_ability": foe.get("ability", ""),
        "own_item": own.get("item", ""), "foe_item": foe.get("item", ""),
        "own_tera": [own.get("tera_type", ""), own.get("terastallized", False)],
        "foe_tera": [foe.get("tera_type", ""), foe.get("terastallized", False)],
        "known_moves": state.get("known_foe_moves", []), "can_tera": state.get("can_tera", False),
        "own_stats": own.get("stats", {}),
    }
    return "v2:" + hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()[:32]


def action_key(candidate) -> str:
    if isinstance(candidate.action, Pokemon):
        return "switch:" + candidate.action.species
    return "move:" + candidate.action.id + (":tera" if candidate.terastallize else "")


class BattleTrace:
    def __init__(self):
        self.decisions: list[dict] = []
        self.pending: dict | None = None
        self.rating_before = None
        self.rating_after = None
        self.opponent_rating = None
        self.protocol_events = 0

    def observe(self, messages: list[list[str]], username: str, role: str | None):
        for parts in messages:
            if len(parts) < 2:
                continue
            kind = parts[1]
            if kind == "player" and len(parts) > 5:
                if parts[5].isdigit():
                    if to_id_str(parts[3]) == to_id_str(username):
                        self.rating_before = int(parts[5])
                    else:
                        self.opponent_rating = int(parts[5])
            elif kind == "raw" and len(parts) > 2:
                clean = html.unescape(re.sub(r"<[^>]*>", "", parts[2]))
                match = re.match(r"(.+?)'s rating: (\d+)\s*→\s*(\d+)", clean)
                if match and match[1] == username:
                    self.rating_before, self.rating_after = int(match[2]), int(match[3])
            if not self.pending:
                continue
            flags = self.pending["observations"]
            ours = bool(role and len(parts) > 2 and parts[2].startswith(role))
            if kind == "move" and len(parts) > 3:
                move_id = to_id_str(parts[3])
                flags["last_move_own" if ours else "last_move_foe"] = move_id
                if not ours and role:
                    seen = flags.setdefault("opponent_moves", [])
                    if len(seen) < 32:
                        seen.append({"actor": _identity(parts[2]), "move": move_id})
            if kind in {"switch", "drag"} and len(parts) > 4:
                actor = _identity(parts[2])
                hp = _condition_hp(parts[4])
                if hp is not None:
                    self.pending["hp_tracker"][actor] = hp
                flags["own_switched" if ours else "foe_switched"] = True
            if kind in {"-damage", "-heal"} and len(parts) > 3:
                actor = _identity(parts[2])
                hp = _condition_hp(parts[3])
                prior = self.pending["hp_tracker"].get(actor)
                if hp is not None:
                    self.pending["hp_tracker"][actor] = hp
                    if prior is not None and kind == "-damage":
                        residual = any("[from]" in item for item in parts[4:])
                        events = flags.setdefault("damage_events", [])
                        if len(events) < 64:
                            events.append({"target": actor, "own": ours,
                                           "amount": round(max(0.0, prior - hp), 6),
                                           "residual": residual,
                                           "move": flags.get("last_move_foe" if ours else "last_move_own", "")})
            if kind in {"move", "switch", "drag", "-damage", "-heal", "faint"}:
                self.protocol_events += 1
                flags["resolved_action"] = True
            if kind == "-crit":
                flags["critical"] = True
            elif kind == "-miss":
                flags["miss"] = True
            elif kind in {"-fail", "cant"}:
                flags["failed"] = True
                if ours:
                    flags["own_failed"] = True
            elif kind == "-immune":
                flags["immune"] = True
                if role and not ours:
                    flags["own_immune"] = True
            elif kind == "error":
                flags["invalid_choice"] = True
            elif kind == "faint":
                flags["own_fainted" if ours else "foe_fainted"] = True
            elif kind in {"-damage", "-heal"} and any("[from]" in item for item in parts[3:]):
                flags["residual_or_item"] = True

    def begin(self, battle, candidates: list, chosen, state: dict):
        actor = chosen.action if isinstance(chosen.action, Pokemon) else battle.active_pokemon
        actor_id = next((key for key, mon in battle.team.items() if mon is actor), "")
        self.pending = {
            "turn": battle.turn, "context_key": context_key(state), "action_key": action_key(chosen),
            "chosen": chosen.label, "score": chosen.score, "features": dict(chosen.features),
            "diagnostics": dict(chosen.diagnostics), "before": state,
            "actor_id": actor_id, "actor_hp": _hp(actor), "observations": {},
            "hp_tracker": {**state["own_hp"], **state["foe_hp_by_id"]},
            "candidates": [{"label": c.label, "score": c.score, "features": dict(c.features),
                            "diagnostics": dict(c.diagnostics)} for c in candidates[:12]],
        }

    def close(self, battle):
        if self.pending is None:
            return
        pending, self.pending = self.pending, None
        after = snapshot(battle)
        before = pending["before"]
        flags = pending["observations"]
        actor_id, foe_id = pending["actor_id"], before["foe_id"]
        actor_after = after["own_hp"].get(actor_id)
        foe_after = after["foe_hp_by_id"].get(foe_id)
        reliable = (actor_after is not None and foe_after is not None and bool(flags.get("resolved_action"))
                    and not flags.get("invalid_choice") and not before["force_switch"]
                    and (after["turn"] > before["turn"] or after["finished"] or
                         (after["force_switch"] and flags.get("own_fainted") and actor_after == 0)))
        # Loss of net HP is observable. It is not an exact per-move damage meter.
        pending["local_outcome"] = {
            "received": max(0, pending["actor_hp"] - actor_after) if actor_after is not None else 0,
            "dealt": max(0, before["foe_hp"] - foe_after) if foe_after is not None else 0,
            "own_ko": actor_after == 0 if actor_after is not None else False,
            "foe_ko": foe_after == 0 if foe_after is not None else False,
            "reliable": reliable,
        }
        pending["after"] = after
        pending.pop("hp_tracker", None)
        pending["local_outcome"]["direct_received"] = sum(
            event["amount"] for event in flags.get("damage_events", [])
            if event["target"] == actor_id and not event["residual"])
        pending["local_outcome"]["direct_dealt"] = sum(
            event["amount"] for event in flags.get("damage_events", [])
            if event["target"] == foe_id and not event["residual"])
        flags["information_revealed"] = (after["foe_id"] == before["foe_id"] and
                                         set(after["known_foe_moves"]) != set(before["known_foe_moves"]))
        self.decisions.append(pending)
