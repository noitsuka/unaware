"""Team game plan, switch discipline and loop breaking.

The simulator and the probabilistic policy measure what an action does this
turn and the next. They cannot see the plan a team was built around: a
suicide lead exists to set the field, an HO team loses tempo with every
voluntary switch, and a stall team wins by status and recovery. This layer
adds those plan values, in the same score units as the policy, and reads the
battle's own transition history to stop repeated plays that only lose HP.

Decisive results (forced wins, certain losses) are never reordered here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from poke_env.battle import Move, MoveCategory, Pokemon

from poke_env.data import GenData

from .engine import _hp, _names, _opponents
from .strategy import PHASING, PIVOTS, SETUP, WEATHER_SETTERS, base_species_id
from .telemetry import action_key

FIELD_HAZARDS = {"stealthrock": "STEALTH_ROCK", "spikes": "SPIKES", "toxicspikes": "TOXIC_SPIKES",
                 "stickyweb": "STICKY_WEB", "stoneaxe": "STEALTH_ROCK", "ceaselessedge": "SPIKES"}
HAZARD_CAP = {"STEALTH_ROCK": 1, "SPIKES": 3, "TOXIC_SPIKES": 2, "STICKY_WEB": 1}
SCREEN_CONDITIONS = {"reflect": "REFLECT", "lightscreen": "LIGHT_SCREEN", "auroraveil": "AURORA_VEIL"}
SACRIFICE_MOVES = {"explosion", "selfdestruct", "memento", "finalgambit", "healingwish", "lunardance", "mistyexplosion"}
STATUS_MOVES = {"toxic", "willowisp", "thunderwave", "spore", "sleeppowder", "glare", "nuzzle", "yawn", "hypnosis",
                "stunspore", "toxicthread"}
RECOVERY = {"recover", "roost", "slackoff", "softboiled", "milkdrink", "shoreup", "synthesis", "moonlight",
            "morningsun", "rest", "healorder", "strengthsap", "wish", "junglehealing", "lifedew"}

# Score units are the policy's: 100 points ~ a full-HP KO-level swing.
SWITCH_COST = {"hyper_offense": 24., "bulky_offense": 16., "balance": 12., "weather": 16.,
               "semi_stall": 9., "stall": 7.}
FIELD_VALUE = {"hyper_offense": 85., "bulky_offense": 48., "balance": 45., "weather": 42.,
               "semi_stall": 45., "stall": 50.}
SETUP_VALUE = {"hyper_offense": 38., "bulky_offense": 26., "balance": 18., "weather": 20.,
               "semi_stall": 12., "stall": 8.}
MAX_PLAN = 110.
REMOVAL_MOVES = {"rapidspin", "defog", "mortalspin", "tidyup", "courtchange"}


def _n(value: Any, default: float = 0.) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _move_id(candidate) -> str:
    return candidate.action.id if isinstance(candidate.action, Move) else ""


def _hazard_layers(battle) -> dict[str, int]:
    layers = {}
    for condition, value in (getattr(battle, "opponent_side_conditions", {}) or {}).items():
        name = getattr(condition, "name", str(condition)).upper()
        layers[name] = max(1, int(_n(value, 1))) if name in {"SPIKES", "TOXIC_SPIKES"} else 1
    return layers


def _magic_bounce(foe) -> bool:
    if foe is None:
        return False
    if foe.ability:
        return foe.ability == "magicbounce"
    return "magicbounce" in {str(a) for a in (getattr(foe, "possible_abilities", None) or [])}


def _foes_alive(battle) -> int:
    return sum(not mon.fainted for mon in _opponents(battle)) or 1


def field_moves_pending(battle, mon) -> list[str]:
    """Field moves this Pokémon can still usefully execute right now."""
    if mon is None:
        return []
    layers = _hazard_layers(battle)
    own = _names(getattr(battle, "side_conditions", {}))
    weather = _names(getattr(battle, "weather", {}))
    pending = []
    for move_id, slot in mon.moves.items():
        if slot.current_pp <= 0:
            continue
        hazard = FIELD_HAZARDS.get(move_id)
        if hazard and layers.get(hazard, 0) < HAZARD_CAP[hazard]:
            # A third Spikes layer rarely beats attacking; two is the plan.
            if hazard == "SPIKES" and layers.get(hazard, 0) >= 2:
                continue
            pending.append(move_id)
        screen = SCREEN_CONDITIONS.get(move_id)
        if screen and screen not in own:
            if move_id == "auroraveil" and not weather & {"SNOW", "SNOWSCAPE", "HAIL"}:
                continue
            pending.append(move_id)
    return pending


def _recent(history: list[dict], limit: int) -> list[dict]:
    return [row for row in history[-limit:] if not (row.get("before") or {}).get("force_switch")]


def _passive_pivot(row: dict) -> bool:
    """U-turn/Volt Switch that barely scratched: in effect, another switch.
    Ladder: runs of 3+ switches/pivots were 2.5x more frequent in long losses."""
    key = str(row.get("action_key", ""))
    if not key.startswith("move:") or key.split(":")[1] not in PIVOTS:
        return False
    outcome = row.get("local_outcome") or {}
    return _n(outcome.get("direct_dealt", outcome.get("dealt"))) < .12 and not outcome.get("foe_ko")


def _is_switch(row: dict) -> bool:
    return str(row.get("action_key", "")).startswith("switch:") or _passive_pivot(row)


def _consecutive_switches(history: list[dict]) -> int:
    count = 0
    for row in reversed(history[-6:]):
        if (row.get("before") or {}).get("force_switch"):
            continue
        if _is_switch(row):
            count += 1
        else:
            break
    return count


def _recent_sacks(history: list[dict], foe) -> int:
    """Voluntary switch-ins this foe KO'd before they acted (the replacement
    request came right after the switch). Ladder: 51 such chains in losses,
    8 in wins; the Pokémon that left usually came back into the same threat."""
    if foe is None:
        return 0
    key, rows, count = base_species_id(foe.species), history[-8:], 0
    for row, following in zip(rows, rows[1:]):
        before = row.get("before") or {}
        if (str(row.get("action_key", "")).startswith("switch:") and not before.get("force_switch")
                and (following.get("before") or {}).get("force_switch")
                and base_species_id(before.get("foe_active", "")) == key):
            count += 1
    return count


def _swing(row: dict) -> float:
    outcome = row.get("local_outcome") or {}
    dealt = _n(outcome.get("direct_dealt", outcome.get("dealt")))
    received = _n(outcome.get("direct_received", outcome.get("received")))
    return dealt - received + .6 * (bool(outcome.get("foe_ko")) - bool(outcome.get("own_ko")))


def likely_ability(mon, ability: str) -> bool:
    """Revealed ability, or the species' only realistic option (Dondozo = Unaware)."""
    if mon is None:
        return False
    if mon.ability:
        return mon.ability == ability
    possible = [str(a) for a in (getattr(mon, "possible_abilities", None) or [])]
    if ability not in possible:
        return False
    common = {"dondozo": "unaware", "clefable": "unaware", "quagsire": "unaware", "clodsire": "unaware",
              "skeledirge": "unaware", "hatterene": "magicbounce", "espeon": "magicbounce"}
    return common.get(base_species_id(mon.species)) == ability or len(possible) == 1


def inferred_choice_lock(history: list[dict], foe) -> str:
    """The attack an opposing Pokémon repeated without switching, item unknown."""
    from poke_env.data import GenData
    if foe is None or (foe.item and foe.item != GenData.from_gen(9).UNKNOWN_ITEM):
        return ""
    key, streak, last = base_species_id(foe.species), 0, ""
    for row in history[-6:]:
        before, flags = row.get("before") or {}, row.get("observations") or {}
        move = flags.get("last_move_foe", "")
        if base_species_id(before.get("foe_active", "")) != key or flags.get("foe_switched") or not move:
            streak, last = 0, ""
            continue
        data = GenData.from_gen(9).moves.get(move, {})
        if data.get("category") == "Status":
            streak, last = 0, ""
            continue
        streak = streak + 1 if move == last else 1
        last = move
    return last if streak >= 2 else ""


def observed_habit(history: list[dict], foe, own=None) -> dict:
    """What the opposing active has kept choosing since it came in.

    The search weighs replies by how good they are for the opponent, so a foe
    that answers Sucker Punch with Quiver Dance/Calm Mind every turn still
    looked certain to attack: Kowtow Cleave seemed suicidal and Sucker Punch
    failed 3-7 times in a row while the foe reached +5 (ladder, 3.11.1).
    Returns {"moves": {id: share}, "weight": mixing weight} or {}."""
    if foe is None:
        return {}
    key = base_species_id(foe.species)
    own_key = base_species_id(own.species) if own is not None else ""
    counts: dict[str, int] = {}
    facing = 0
    for row in reversed(history[-8:]):
        before, flags = row.get("before") or {}, row.get("observations") or {}
        if base_species_id(before.get("foe_active", "")) != key:
            break
        move = flags.get("last_move_foe", "")
        if move:
            counts[move] = counts.get(move, 0) + 1
            facing += base_species_id(before.get("active", "")) == own_key
        if flags.get("foe_switched"):
            break
    total = sum(counts.values())
    top = max(counts.values(), default=0)
    if total < 2 or top < 2:
        return {}
    weight = min(.85, .2 * top + .1)
    if not facing:
        weight *= .5  # the habit was formed against another Pokémon of ours
    return {"moves": {move: count / total for move, count in counts.items()}, "weight": round(weight, 3)}


def hazard_burden(battle, engine=None) -> float:
    """Expected entry damage on our side, summed over the healthy bench."""
    names = {getattr(k, "name", str(k)).upper(): v for k, v in (getattr(battle, "side_conditions", {}) or {}).items()}
    if not names.keys() & {"STEALTH_ROCK", "SPIKES", "TOXIC_SPIKES", "STICKY_WEB"}:
        return 0.
    total = 0.
    for mon in getattr(battle, "team", {}).values():
        if mon.fainted or mon is getattr(battle, "active_pokemon", None) or mon.item == "heavydutyboots":
            continue
        if engine is not None:
            total += engine._hazards(mon, battle)
        else:
            total += .125 * ("STEALTH_ROCK" in names) + (.125, 1 / 6, .25)[min(3, max(1, int(names.get("SPIKES", 1)))) - 1] * ("SPIKES" in names)
        total += .06 * ("TOXIC_SPIKES" in names and not mon.status)
    return total


def foe_disabled(foe) -> bool:
    """Frozen, or asleep without Sleep Talk: the target cannot act this turn
    (a 20% thaw is not a threat to plan around)."""
    if foe is None or foe.status is None:
        return False
    name = getattr(foe.status, "name", "")
    if name == "FRZ":
        return True
    return name == "SLP" and "sleeptalk" not in foe.moves


def guaranteed_ko(candidate, foe) -> bool:
    """Minimum roll KOs the target if it stays in (static damage, not the
    switch-averaged search estimate)."""
    if foe is None or not isinstance(candidate.action, Move):
        return False
    diagnostic = candidate.diagnostics
    low, accuracy = _n(diagnostic.get("damage_low")), _n(diagnostic.get("accuracy"), 1.)
    return low > 0 and low >= _hp(foe) + .005 and accuracy >= .9


# A defensive Tera can turn these attacking types into an immunity
# (Ground -> Flying, Dragon -> Fairy...). The other types hit every Tera.
TERA_IMMUNE_TYPES = {"NORMAL", "FIGHTING", "GROUND", "ELECTRIC", "PSYCHIC", "POISON", "GHOST", "DRAGON"}


def tera_proof(candidate) -> bool:
    move = candidate.action
    return getattr(getattr(move, "type", None), "name", "") not in TERA_IMMUNE_TYPES


def foe_can_tera(battle, foe) -> bool:
    return (foe is not None and not getattr(foe, "is_terastallized", False)
            and not getattr(battle, "opponent_used_tera", True))


def gain_is_small(plain, diagnostic: dict, risk: float) -> bool:
    """The Tera version neither saves the user nor secures a KO (<60% gain)."""
    if plain is None:
        return True
    survival = _n(plain.diagnostics.get("faint_risk")) - risk
    ko = _n(diagnostic.get("ko_chance")) - _n(plain.diagnostics.get("ko_chance"))
    return max(survival, ko) < .6


def _burned_physical(mon) -> bool:
    """Burn halves physical damage: such an attacker no longer wins the game."""
    if mon is None or getattr(getattr(mon, "status", None), "name", "") != "BRN" or mon.ability == "guts":
        return False
    kinds = [slot.category for slot in mon.moves.values() if slot.category != MoveCategory.STATUS]
    return bool(kinds) and sum(kind == MoveCategory.PHYSICAL for kind in kinds) * 2 >= len(kinds)


def _signature(state: dict) -> tuple:
    bucket = lambda value: int(_n(value) * 5)
    return (state.get("active"), state.get("foe_active"), bucket(state.get("active_hp")),
            bucket(state.get("foe_hp")))


@dataclass
class _Situation:
    battle: Any
    candidates: list
    profile: dict
    history: list[dict]
    forced: bool
    own: Any
    foe: Any
    style: str
    roles: dict[str, set]
    own_roles: set
    own_id: str
    moves: list
    disabled: bool
    risk_scale: float
    stay_risk: float
    stay_damage: float
    secure_ko: float
    robust_stay: float
    tempo_scale: float
    pending: list[str]
    lead: bool
    bounce: bool
    foes_left: int
    consecutive: int
    sacks: int
    sucker_fails: int
    recent_switches: int
    alive: int
    conditions: set
    endangered_key: bool
    remover_active: bool
    proof_ko: bool
    last: dict | None
    last_foe_move: str
    foe_setup: bool
    signature_hits: list[dict]
    matchup_memory: dict | None
    winplan: Any

    def risk(self, candidate) -> float:
        return _n(candidate.diagnostics.get("faint_risk")) * self.risk_scale

    def weather_missing(self, setter: str) -> bool:
        climate = self.profile.get("weather", "")
        current = {name.lower() for name in _names(getattr(self.battle, "weather", {}))}
        return WEATHER_SETTERS.get(setter) == climate and not any(climate in name for name in current)


class GamePlan:
    """Plan values and history-aware corrections for one decision."""

    def __init__(self, principles: bool = True, refined: bool = True):
        # False = pre-3.11 behaviour (no key-piece/hazard-war/switch-dance rules), for A/B duels.
        self.principles = principles
        # False = 3.11.0 behaviour (no worst-case/Tera/burn checks on KOs and key pieces).
        self.refined = principles and refined
        self.rules = (self._switch_discipline, self._move_plan, self._weather_switch_in, self._loop_breaking,
                      self._matchup_memory, self._move_tactics, self._hazard_relief, self._win_plan, self._tera)

    @staticmethod
    def _acts(candidate, disabled: bool, risk_scale: float) -> bool:
        """The move resolves before we can be knocked out: we move first, the
        target cannot act, or even the worst simulated reply leaves us standing.
        Moving second, the switch-averaged risk hides the line where the foe
        stays in and hits first."""
        diagnostic = candidate.diagnostics
        risk = _n(diagnostic.get("faint_risk")) * risk_scale
        worst = _n(diagnostic.get("search_faint_risk_worst", diagnostic.get("faint_risk"))) * risk_scale
        return (_n(diagnostic.get("policy_first_probability"), .5) >= .99 or disabled
                or (risk <= .15 and worst <= .3))

    def apply(self, battle, candidates: list, profile: dict | None, opponent_profile: dict | None,
              history: list[dict] | None = None, state: dict | None = None,
              matchup_memory: dict | None = None, winplan=None) -> list:
        if len(candidates) < 2:
            return candidates
        s = self._read(battle, candidates, profile or {}, list(history or []), state or {}, matchup_memory, winplan)
        for candidate in candidates:
            if candidate.diagnostics.get("search_legal") == 0 or candidate.score >= 5000 or candidate.score <= -5000:
                continue
            value, reasons = 0., []
            for rule in self.rules:
                value += rule(s, candidate, reasons)
            value = max(-MAX_PLAN, min(MAX_PLAN, value))
            if value:
                candidate.score += value
                candidate.diagnostics["plan_adjustment"] = round(value, 4)
                candidate.reasons[:0] = reasons
        return sorted(candidates, key=lambda c: (-c.score, c.terastallize, c.label))

    def _read(self, battle, candidates, profile, history, state, matchup_memory, winplan) -> _Situation:
        forced = getattr(battle, "force_switch", False)
        forced = any(forced) if isinstance(forced, (list, tuple)) else bool(forced)
        own, foe = getattr(battle, "active_pokemon", None), getattr(battle, "opponent_active_pokemon", None)
        style = profile.get("archetype", "balance")
        roles = {base_species_id(row["species"]): set(row.get("roles", [])) for row in profile.get("roles", [])}
        own_roles = roles.get(base_species_id(own.species), set()) if own else set()
        moves = [c for c in candidates if isinstance(c.action, Move) and c.diagnostics.get("search_legal") != 0]
        disabled = foe_disabled(foe)
        # Rare chance events (thaw, full paralysis) are over-sampled by the
        # search's two antithetic rolls; a disabled target barely threatens.
        risk_scale = .3 if disabled else 1.
        stay_risk = min((_n(c.diagnostics.get("faint_risk")) for c in moves), default=1.) * risk_scale
        stay_damage = max((_n(c.diagnostics.get("expected_dealt")) for c in moves), default=0.)
        # A secured KO: high KO chance and little risk of being hit first.
        secure_ko = max((_n(c.diagnostics.get("ko_chance")) for c in moves
                         if _n(c.diagnostics.get("faint_risk")) * risk_scale <= .25), default=0.)
        if any(guaranteed_ko(c, foe) and (not self.refined or self._acts(c, disabled, risk_scale)) for c in moves):
            secure_ko = max(secure_ko, .9)
        # Staying in has a price too: the tempo penalties on switching fade when
        # every way of staying is likely to faint (real games: 14% of our
        # voluntary switch-ins died without acting, often after being held in).
        robust_stay = min((max(_n(c.diagnostics.get("faint_risk")), .5 * _n(c.diagnostics.get("search_faint_risk_worst")))
                           for c in moves), default=1.) * risk_scale
        tempo_scale = max(0., 1. - robust_stay) if self.refined and robust_stay >= .4 else 1.
        pending = field_moves_pending(battle, own) if own and not forced else []
        lead = "suicide_lead" in own_roles or (style == "hyper_offense" and pending and
                                               ("screens" in own_roles or "web_setter" in own_roles))
        # Sucker Punch read and refused: the foe keeps choosing non-attacks.
        sucker_fails = sum(1 for row in _recent(history, 4)
                           if str(row.get("action_key", "")).split(":")[1:2] in (["suckerpunch"], ["thunderclap"])
                           and (row.get("observations") or {}).get("own_failed")) if self.refined else 0
        # Key pieces (Smogon fundamentals): a weather team without its setter
        # loses its plan; a win condition is not traded early for chip damage.
        alive = sum(not mon.fainted for mon in getattr(battle, "team", {}).values())
        setters = {base_species_id(name) for name in profile.get("weather_setters", [])} if style == "weather" else set()
        conditions = {base_species_id(name) for name in profile.get("win_conditions", [])}
        own_id = base_species_id(own.species) if own is not None else ""
        key_piece = own is not None and not forced and (
            own_id in setters or (own_id in conditions and alive >= 4 and not (self.refined and _burned_physical(own))))
        best_stay_ko = max((_n(c.diagnostics.get("ko_chance")) for c in moves), default=0.)
        # Hazard war: a revealed remover on the field just undoes new hazards.
        remover_active = self.principles and foe is not None and _hp(foe) > .35 and bool(
            {"defog", "rapidspin", "mortalspin", "courtchange", "tidyup"} & set(foe.moves))
        # A KO that a defensive Tera cannot dodge beats one that it can.
        tera_risk = self.refined and foe_can_tera(battle, foe)
        last = history[-1] if history else None
        # Opposing setup in progress: boosts on the field or a boosting move last turn.
        last_foe_move = ((last or {}).get("observations") or {}).get("last_move_foe", "")
        foe_setup = self.refined and foe is not None and (
            sum(max(0, foe.boosts.get(stat, 0)) for stat in ("atk", "spa", "spe")) >= 1
            or last_foe_move in SETUP or (last_foe_move == "curse" and "GHOST" not in {t.name for t in foe.types}))
        signature_hits = [row for row in history[-10:] if not (row.get("before") or {}).get("force_switch")
                          and _signature(row.get("before") or {}) == _signature(state)] if state else []
        return _Situation(
            battle=battle, candidates=candidates, profile=profile, history=history, forced=forced,
            own=own, foe=foe, style=style, roles=roles, own_roles=own_roles, own_id=own_id, moves=moves,
            disabled=disabled, risk_scale=risk_scale, stay_risk=stay_risk, stay_damage=stay_damage,
            secure_ko=secure_ko, robust_stay=robust_stay, tempo_scale=tempo_scale, pending=pending, lead=lead,
            bounce=_magic_bounce(foe), foes_left=_foes_alive(battle), consecutive=_consecutive_switches(history),
            sacks=_recent_sacks(history, foe) if self.principles else 0, sucker_fails=sucker_fails,
            recent_switches=sum(_is_switch(row) for row in _recent(history, 6)), alive=alive, conditions=conditions,
            endangered_key=self.principles and key_piece and stay_risk >= .5 and best_stay_ko < .7,
            remover_active=remover_active,
            proof_ko=tera_risk and any(guaranteed_ko(c, foe) and tera_proof(c) for c in moves),
            last=last, last_foe_move=last_foe_move, foe_setup=foe_setup, signature_hits=signature_hits,
            matchup_memory=matchup_memory, winplan=winplan)

    def _switch_discipline(self, s: _Situation, candidate, reasons: list) -> float:
        if not isinstance(candidate.action, Pokemon) or s.forced or s.own is None or s.own.fainted:
            return 0.
        value = -SWITCH_COST.get(s.style, 12.) * s.tempo_scale
        risk = s.risk(candidate)
        # Repeated voluntary switching hands the opponent free turns.
        if s.consecutive:
            value -= 16. * s.consecutive * s.tempo_scale
            reasons.append(f"{s.consecutive} switch(es) in a row without attacking; every extra switch gives up tempo.")
        if self.principles and s.recent_switches >= 2:
            value -= 12. * (s.recent_switches - 1) * s.tempo_scale
            reasons.append(f"{s.recent_switches} switches in recent turns: the switch dance hands over the initiative.")
        if s.tempo_scale < 1.:
            reasons.append(f"Staying in risks fainting ({s.robust_stay:.0%}); the tempo cost of switching matters less.")
        if s.endangered_key and risk <= .25:
            value += 45.
            reasons.append("Saves a key piece of the plan (weather setter/win condition) that would faint now.")
        if (s.last and str(s.last.get("action_key", "")).startswith("switch:")
                and base_species_id((s.last.get("before") or {}).get("active", "")) == base_species_id(candidate.action.species)):
            value -= 28.
            reasons.append("Would bring back the Pokémon that just left (switch ping-pong).")
        if s.secure_ko >= .85:
            value -= 35.
            reasons.append("A near-certain knockout is available; switching wastes the opening.")
        if (s.robust_stay if self.refined else s.stay_risk) < .3 and s.stay_damage >= .2:
            value -= 20. if s.style in {"hyper_offense", "bulky_offense", "weather"} else 12.
            reasons.append("The active Pokémon is not threatened and deals useful damage; no need to switch.")
        boosts = sum(max(0, s.own.boosts.get(stat, 0)) for stat in ("atk", "spa", "spe"))
        if boosts >= 2 and s.stay_risk < .5:
            value -= 8. * boosts
            reasons.append("Switching would throw away the boosts.")
        if s.sacks and risk >= .85 and s.stay_damage >= .1:
            value -= 20. * s.sacks
            reasons.append(f"This opponent already knocked out {s.sacks} of our Pokémon on the switch-in; another sack does not change the matchup.")
        if s.lead and s.pending:
            value -= 45.
            reasons.append("The lead has not set up the field yet; leaving now wastes its job.")
        elif "suicide_lead" in s.own_roles:
            value -= 12.
            reasons.append("The suicide lead is expendable; preserving it costs the team tempo.")
        return value

    def _move_plan(self, s: _Situation, candidate, reasons: list) -> float:
        if isinstance(candidate.action, Pokemon) or s.own is None:
            return 0.
        value, diagnostic, foe = 0., candidate.diagnostics, s.foe
        move = candidate.action
        move_id = move.id
        risk = s.risk(candidate)
        ko = _n(diagnostic.get("ko_chance"))
        first = _n(diagnostic.get("policy_first_probability"), .5)
        # Moving second, the switch-averaged risk hides the line where the
        # foe stays and hits first; the worst case must be safe too.
        if guaranteed_ko(candidate, foe) and (self._acts(candidate, s.disabled, s.risk_scale) if self.refined
                                              else first >= .99 or s.disabled or risk <= .15):
            # The opponent may switch, but a certain KO on the Pokémon in
            # front of us is worth more than a guess about the switch-in.
            if s.proof_ko and not tera_proof(candidate):
                value += 15.
                reasons.append("Guaranteed KO, but the opponent's Tera could flip the typing; a Tera-proof KO exists.")
            else:
                value += 55.
                reasons.append("Guaranteed knockout on the current target (min roll is enough); finish it.")
        elif ko >= .85 and risk <= .25:
            value += 30. * ko
            reasons.append(f"Finishes the opponent ({ko:.0%}) at little risk.")
        if (s.sucker_fails >= 2 and move_id not in {"suckerpunch", "thunderclap"}
                and guaranteed_ko(candidate, foe)):
            # Frosmoth at 2% reached +5 while Sucker Punch failed six times:
            # the reply the search fears is the one this player never makes.
            value += 60. + 30. * s.sucker_fails
            reasons.append(f"Sucker Punch failed {s.sucker_fails}x: the opponent is not attacking; the regular move knocks out.")
        if s.endangered_key:
            value -= 30.
            reasons.append("A key piece of the plan is in danger: attacking here sacks it for nothing.")
        if move_id in FIELD_HAZARDS and s.remover_active and move.category == MoveCategory.STATUS:
            value -= 45.
            reasons.append("The active opponent has shown hazard removal; setting them again now wastes the turn.")
        # Field preparation: the purpose of leads and hazard setters.
        if move_id in s.pending and not (s.bounce and move.category == MoveCategory.STATUS):
            hazard = FIELD_HAZARDS.get(move_id)
            layers = _hazard_layers(s.battle).get(hazard, 0) if hazard else 0
            base = FIELD_VALUE.get(s.style, 45.) * (1. if s.lead else .6)
            base *= (1., .6, .35)[min(2, layers)]
            if hazard:
                base *= min(1., .35 + s.foes_left / 6)
            # The lead must still act; a slower lead that dies first sets nothing.
            resolve = 1 - .7 * min(1., risk)
            if move.category != MoveCategory.STATUS and s.lead:
                resolve *= .8
            value += base * resolve
            reasons.append("Setting up the field is this Pokémon's job in the team plan.")
        elif s.lead and s.pending and move.category != MoveCategory.STATUS and ko < .7:
            value -= 38. if s.style == "hyper_offense" else 18.
            reasons.append("The lead is attacking before setting hazards/screens.")
        if s.lead and not s.pending and move_id in SACRIFICE_MOVES:
            value += 30.
            reasons.append("Field is set: the lead can be sacked for a free switch-in.")
        if s.lead and move_id == "taunt" and foe is not None and not ("TAUNT" in _names(foe.effects)):
            foe_status = sum(slot.category == MoveCategory.STATUS for slot in foe.moves.values())
            value += 10. + 8. * min(2, foe_status)
        # Setup with real room to act afterwards.
        if move_id in SETUP:
            worst = _n(diagnostic.get("search_faint_risk_worst", risk)) * s.risk_scale
            incoming = _n(diagnostic.get("search_received_worst", diagnostic.get("expected_received")))
            attack_boost = max(s.own.boosts.get("atk", 0), s.own.boosts.get("spa", 0))
            if (worst < .25 and incoming < .5 * _hp(s.own) and attack_boost < 4
                    and _n(candidate.features.get("policy_setup")) > 0):
                value += SETUP_VALUE.get(s.style, 18.) * (1. if attack_boost < 2 else .45)
                reasons.append("Safe window for the sweeper to set up.")
                if s.disabled and not any(guaranteed_ko(c, foe) for c in s.moves):
                    value += 20.
                    reasons.append("Target frozen/asleep: a free turn to boost.")
            elif worst >= .5:
                value -= 20.
                reasons.append("Setup would faint before the boost pays off.")
        # Defensive plans: status, recovery and phazing make progress.
        if s.style in {"stall", "semi_stall", "balance"}:
            foe_status = foe.status if foe is not None else None
            if move_id in STATUS_MOVES and foe is not None and not foe_status and _n(candidate.features.get("policy_status")) > .05:
                value += 30. if s.style != "balance" else 15.
                reasons.append("Status makes lasting progress for the defensive plan.")
            if move_id in RECOVERY:
                hp = _hp(s.own)
                if hp < .55:
                    value += 30. if s.style != "balance" else 18.
                    reasons.append("Recovering now keeps the wall alive.")
                elif hp > .85:
                    value -= 22.
            if move_id in PHASING and foe is not None and sum(max(0, b) for b in foe.boosts.values()) >= 1:
                value += 40.
                reasons.append("Removing the opponent's boosts comes first.")
        if move_id in PIVOTS and s.style in {"balance", "bulky_offense", "hyper_offense"} and s.battle.available_switches:
            if _n(diagnostic.get("expected_dealt")) > .08 or move.category == MoveCategory.STATUS:
                value += 12.
        if (move_id in PIVOTS and self.principles and s.recent_switches >= 2
                and _n(diagnostic.get("expected_dealt")) < .12):
            value -= 12. * (s.recent_switches - 1) * s.tempo_scale
            reasons.append(f"{s.recent_switches} switches/pivots without damage in recent turns: another pivot only gives up tempo.")
        if s.style == "weather" and s.weather_missing(move_id):
            value += 25.
        return value

    def _weather_switch_in(self, s: _Situation, candidate, reasons: list) -> float:
        if (isinstance(candidate.action, Pokemon) and s.style == "weather" and not s.forced
                and s.profile.get("weather", "") and s.weather_missing(candidate.action.ability or "")):
            return 22.
        return 0.

    def _loop_breaking(self, s: _Situation, candidate, reasons: list) -> float:
        """The same play in the same matchup that keeps losing HP without
        progress is penalised more each time."""
        if not s.history or s.own is None or s.forced:
            return 0.
        value, diagnostic, key = 0., candidate.diagnostics, action_key(candidate)
        foe_id = base_species_id(s.foe.species if s.foe else "")
        same = [row for row in _recent(s.history, 8)
                if row.get("action_key") == key
                and base_species_id((row.get("before") or {}).get("active", "")) == base_species_id(s.own.species)
                and base_species_id((row.get("before") or {}).get("foe_active", "")) == foe_id
                and (row.get("local_outcome") or {}).get("reliable")]
        productive = any(_n(candidate.features.get("policy_" + name)) > .05 for name in ("heal", "hazards", "removal", "status"))
        if same and not productive and _n(diagnostic.get("ko_chance")) < .7:
            swings = [_swing(row) for row in same]
            average = sum(swings) / len(swings)
            if average <= .02:
                value -= 14. * len(same) + 40. * max(0., -average)
                reasons.append(f"Same move {len(same)}x in this matchup without progress; break the loop.")
        # Stalemate: the move keeps hitting but the target recovers it
        # all back (Recover, Regenerator, Leftovers). Damage is not progress.
        streak = [row for row in _recent(s.history, 6) if row.get("action_key") == key
                  and base_species_id((row.get("before") or {}).get("foe_active", "")) == foe_id]
        if len(streak) >= 2 and s.foe is not None and _n(diagnostic.get("ko_chance")) < .5:
            first_hp = _n((streak[0].get("before") or {}).get("foe_hp"), 1.)
            if _hp(s.foe) >= first_hp - .08:
                value -= 22. * (len(streak) - 1)
                reasons.append("The target heals all of this move's damage; try setup, status or another line.")
        if len(s.signature_hits) >= 2:
            tried = sum(row.get("action_key") == key for row in s.signature_hits)
            if tried:
                value -= 10. * tried
                reasons.append("Repeated position: vary the move to break the cycle.")
        return value

    def _matchup_memory(self, s: _Situation, candidate, reasons: list) -> float:
        if not s.matchup_memory or s.own is None or s.foe is None:
            return 0.
        bias = s.matchup_memory.get((base_species_id(s.own.species), base_species_id(s.foe.species), action_key(candidate)))
        if not bias:
            return 0.
        reasons.append(bias["reason"])
        return bias["bias"]

    def _move_tactics(self, s: _Situation, candidate, reasons: list) -> float:
        if isinstance(candidate.action, Pokemon) or s.own is None:
            return 0.
        value, diagnostic, foe, history = 0., candidate.diagnostics, s.foe, s.history
        move_id = candidate.action.id
        # Hazards on our side tax every switch: clear them early.
        burden = hazard_burden(s.battle)
        if move_id in REMOVAL_MOVES and burden >= .2 and _n(diagnostic.get("search_legal"), 1) != 0:
            works = not (move_id == "defog" and foe is not None and foe.ability == "goodasgold")
            works &= not (move_id in {"rapidspin", "mortalspin"} and _n(diagnostic.get("expected_dealt")) <= 0
                          and foe is not None and "GHOST" in {t.name for t in foe.types})
            if works:
                value += min(70., 55. * burden)
                reasons.append(f"Hazards on our side cost ~{burden:.0%} total HP over the next switch-ins; remove them now.")
        # Healing at (almost) full HP wastes the turn in every style.
        if move_id in RECOVERY and move_id != "wish" and _hp(s.own) >= .9 and _n(diagnostic.get("expected_received")) < .15:
            value -= 30.
            reasons.append("HP almost full: recovering now wastes the turn.")
        # Boosting into Unaware gains nothing.
        if move_id in SETUP and likely_ability(foe, "unaware"):
            value -= 45.
            reasons.append("The target probably has Unaware: boosts are ignored.")
        # Against setup in progress, passive turns are free boosts for the foe.
        if s.foe_setup:
            if move_id in RECOVERY and _hp(s.own) >= .5 and _n(diagnostic.get("ko_chance")) < .5:
                value -= 25.
                reasons.append("The opponent is boosting; recovering now only gives it more turns.")
            if move_id in PHASING and move_id != "encore" or (move_id == "encore" and s.last_foe_move in SETUP | {"curse"}):
                value += 25.
                reasons.append("Undoes the opponent's setup (phazing/Haze/Encore).")
        if move_id not in {"suckerpunch", "thunderclap"} or foe is None:
            return value
        # Sucker Punch fails into Protect, setup and status moves.
        if self.refined:
            fails = sum(1 for row in _recent(history, 4) if str(row.get("action_key", "")).startswith("move:" + move_id)
                        and (row.get("observations") or {}).get("own_failed"))
            status_last = bool(s.last_foe_move) and (GenData.from_gen(9).moves.get(s.last_foe_move) or {}).get("category") == "Status"
            if fails >= 2:
                value -= 90.
                reasons.append(f"Sucker Punch failed {fails}x in a few turns: the opponent is not attacking.")
            elif status_last and _n(diagnostic.get("ko_chance")) < .95:
                value -= 30.
                reasons.append("The opponent used a status move last turn; Sucker Punch would fail again.")
        failed_before = bool(history) and history[-1].get("action_key", "").startswith("move:" + move_id) and (history[-1].get("observations") or {}).get("own_failed")
        status_options = sum(slot.category == MoveCategory.STATUS for slot in foe.moves.values())
        if failed_before:
            value -= 35.
            reasons.append("Sucker Punch just failed; the opponent is reading it.")
        elif status_options and _n(diagnostic.get("ko_chance")) < .9:
            value -= 8. * min(3, status_options)
        if s.last_foe_move in {"protect", "detect", "banefulbunker", "spikyshield", "kingsshield", "silktrap"}:
            value -= 5.
        return value

    def _hazard_relief(self, s: _Situation, candidate, reasons: list) -> float:
        if not isinstance(candidate.action, Pokemon) or s.forced or s.own is None:
            return 0.
        burden = hazard_burden(s.battle)
        if burden and "hazard_removal" in s.roles.get(base_species_id(candidate.action.species), set()):
            return min(25., 25. * burden)
        return 0.

    def _win_plan(self, s: _Situation, candidate, reasons: list) -> float:
        if s.winplan is None:
            return 0.
        from .winplan import plan_value
        extra, why = plan_value(s.winplan, s.battle, candidate, s.foe, s.own, candidate.diagnostics,
                                isinstance(candidate.action, Pokemon), s.forced)
        reasons.extend(why)
        return extra

    def _tera(self, s: _Situation, candidate, reasons: list) -> float:
        """Tera is spent once per battle. It must buy a real difference in
        survival or KO odds over the same move without it."""
        if not candidate.terastallize or isinstance(candidate.action, Pokemon):
            return 0.
        value, diagnostic, risk = 0., candidate.diagnostics, s.risk(candidate)
        plain = next((c for c in s.candidates if not c.terastallize and isinstance(c.action, Move)
                      and c.action.id == candidate.action.id), None)
        if plain is not None:
            survival_gain = _n(plain.diagnostics.get("faint_risk")) - risk
            ko_gain = _n(diagnostic.get("ko_chance")) - _n(plain.diagnostics.get("ko_chance"))
            gain = max(survival_gain, ko_gain)
            if gain < .3:
                value -= 40. if candidate.action.category == MoveCategory.STATUS else 30.
                reasons.append("Terastallizing here does not change the outcome; save the Tera.")
            elif self.refined and s.alive >= 5 and s.foes_left >= 5 and gain < .5:
                # Top-ladder players Tera around turn 15 (median), the bot did
                # on turn 4: an early Tera rarely decides the game it spends.
                value -= 25.
                reasons.append("Early game: the Tera is worth more saved for later; the gain here is moderate.")
        if self.refined and s.own_id not in s.conditions and gain_is_small(plain, diagnostic, risk) and any(
                base_species_id(mon.species) in s.conditions and not mon.fainted
                for mon in getattr(s.battle, "team", {}).values()):
            # Ladder: Tera on Slowking (not a win condition) went 20-38.
            value -= 35.
            reasons.append("The team's Tera belongs to the win condition that is still alive; do not spend it on another Pokémon.")
        return value
