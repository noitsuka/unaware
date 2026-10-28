"""Probabilistic, team-conditioned singles policy.

Mechanics and hidden-set sampling belong to the official simulator. This
module owns the final utility: its score is rebuilt from observable tactical
features, not inherited from the old evaluator. Search supplies outcome
probabilities and a bounded continuation term. No network or disk I/O occurs
on the decision path; learned coefficients are immutable session snapshots.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from poke_env.battle import Field, Move, MoveCategory, Pokemon, PokemonType, Status

from .engine import Candidate, DecisionEngine, _Arena, _hp, _names, _opponents, _stage
from .strategy import PHASING, REMOVAL, SCREENS, SETUP, WEATHER_SETTERS, base_species_id

VERSION = 4
WEIGHTS = {
    "damage": 100., "ko": 115., "incoming": -68., "faint_risk": -140.,
    "heal": 82., "setup": 38., "hazards": 70., "removal": 72.,
    "status": 42., "tempo": 16., "matchup": 34., "switch": -8.,
    "tera": -7., "resource": -40., "preserve": 32., "weather": 42.,
    "residual": 74., "denial": 30., "uncertainty": -12.,
}
STYLE_WEIGHTS = {
    "hyper_offense": {"damage": 118., "ko": 135., "incoming": -52., "faint_risk": -108.,
                      "setup": 52., "switch": -15., "hazards": 83., "heal": 60., "tempo": 23.},
    "bulky_offense": {"damage": 108., "ko": 125., "matchup": 40., "tempo": 21., "setup": 42.},
    "stall": {"damage": 77., "incoming": -90., "faint_risk": -174., "heal": 110.,
              "removal": 96., "status": 66., "hazards": 82., "switch": -3., "setup": 22.},
    "semi_stall": {"incoming": -82., "faint_risk": -160., "heal": 100., "removal": 85.,
                   "status": 55., "switch": -5., "preserve": 43.},
    "balance": {},
    "weather": {"damage": 112., "tempo": 22., "weather": 66., "preserve": 40., "switch": -9.},
}
LEARNABLE = tuple(WEIGHTS)
MAX_WEIGHT_DELTA = 6.
MAX_LEARNED_UTILITY = 10.
CLIMATE_NAMES = {"RAINDANCE": "rain", "SUNNYDAY": "sun", "SANDSTORM": "sand",
                 "SNOW": "snow", "SNOWSCAPE": "snow", "HAIL": "snow"}


def finite(value: Any, default: float = 0.) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def unit(value: Any) -> float:
    return min(1., max(0., finite(value)))


def policy_context(battle: Any, profile: dict, opponent_profile: dict) -> str:
    """Stable coarse contexts keep defensive and offensive evidence separate."""
    own, foe = battle.active_pokemon, battle.opponent_active_pokemon
    alive = sum(not mon.fainted for mon in getattr(battle, "team", {}).values())
    other = opponent_profile.get("archetype", "unknown") if opponent_profile.get("confidence", 0) >= .4 else "unknown"
    payload = [VERSION, profile.get("archetype", "balance"), other,
               base_species_id(own.species) if own else "", base_species_id(foe.species) if foe else "",
               "endgame" if alive <= 2 else "midgame", min(2, int(_hp(own) * 3)),
               min(2, int(_hp(foe) * 3)), bool(getattr(battle, "force_switch", False)),
               getattr(own.status, "name", "") if own else "",
               getattr(foe.status, "name", "") if foe else ""]
    return "p4:" + hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:28]


class BattlePolicy:
    """Rebuild action values from probability-weighted, inspectable features."""

    # Weight of the simulator's position value vs the tactical features.
    # A/B duels (100 mirror games) showed no gain from a larger
    # search weight (46% vs the current blend), so the measured blend stays:
    # the search feeds probabilities/outcomes into the features instead.
    search_weight = 0.0
    utility_weight = 1.0

    def rank(self, battle: Any, candidates: list[Candidate], engine: DecisionEngine,
             profile: dict | None = None, opponent_profile: dict | None = None,
             learned_weights: dict | None = None) -> list[Candidate]:
        if not candidates:
            return []
        profile, opponent_profile = profile or {}, opponent_profile or {}
        style = profile.get("archetype", "balance")
        weights = {**WEIGHTS, **STYLE_WEIGHTS.get(style, {})}
        # A revealed defensive opponent increases the value of lasting progress;
        # incomplete preview inference can never supply a full style adjustment.
        confidence = unit(opponent_profile.get("confidence"))
        if opponent_profile.get("archetype") in {"stall", "semi_stall"}:
            weights["hazards"] += 9 * confidence
            weights["denial"] += 8 * confidence
            weights["setup"] += 6 * confidence
        elif opponent_profile.get("archetype") == "hyper_offense":
            weights["ko"] += 10 * confidence
            weights["tempo"] += 5 * confidence
        context = policy_context(battle, profile, opponent_profile)
        # Value weights are no longer learned. The former residual learning
        # lowered the value of damage/KO whenever a forecast missed (a miss or a
        # low roll), so the bot attacked less the more it played. Learning now
        # acts on observed matchup outcomes (GamePlan), never on what a KO is worth.
        learned = {}
        roles = {base_species_id(row["species"]): set(row.get("roles", [])) for row in profile.get("roles", [])}
        endangered = min((unit(c.diagnostics.get("faint_risk")) for c in candidates if isinstance(c.action, Move)), default=0.)
        for candidate in candidates:
            diagnostic = candidate.diagnostics
            if diagnostic.get("search_legal") == 0:
                candidate.score = -1e6
                continue
            vector, extra = self.features(battle, candidate, engine, profile, roles, endangered)
            utility = sum(weights[key] * value for key, value in vector.items())
            adaptive = sum(max(-MAX_WEIGHT_DELTA, min(MAX_WEIGHT_DELTA, finite(learned.get(key)))) * value
                           for key, value in vector.items())
            adaptive = max(-MAX_LEARNED_UTILITY, min(MAX_LEARNED_UTILITY, adaptive))
            # The official simulation's position value (who survives, boosts,
            # hazards, the next matchup, under the opponent model) is the
            # backbone of the score; tactical features complement it. Before
            # 3.10 it was squeezed into +-12 points and the hand-made features
            # decided almost everything.
            continuation = 0.
            searched = diagnostic.get("search_legal") == 1 and "search_score" in diagnostic
            if searched and self.search_weight:
                continuation = self.search_weight * max(-300., min(300., finite(diagnostic.get("search_score"))))
                utility_part = self.utility_weight * utility
            else:
                if finite(diagnostic.get("search_depth")) >= 2:
                    continuation = 12 * math.tanh(finite(diagnostic.get("search_mean", diagnostic.get("search_score"))) / 180)
                utility_part = utility
            win = unit(diagnostic.get("search_win_probability"))
            loss = unit(diagnostic.get("search_loss_probability"))
            score = utility_part + adaptive + continuation + 220 * (win - loss)
            worst = finite(diagnostic.get("search_worst"))
            if worst >= 5000 or win >= .999:
                # A won position must be closed out quickly: every extra turn
                # gives the opponent crits, misses and timer chances.
                score = 10000 + 60 * vector["ko"] + 30 * vector["damage"] - 20 * vector["switch"] + utility * .01
            elif loss >= .999 or (worst < -9000 and finite(diagnostic.get("search_score")) < -8500):
                score = -10000 + utility * .01
            # Emergency forced replacement still values the best reachable next
            # attack, rather than crediting damage on its free entry turn.
            candidate.score = round(score, 6)
            candidate.features.update({"policy_" + key: round(value, 6) for key, value in vector.items()})
            candidate.diagnostics.update({"policy_version": VERSION, "policy_context": context,
                "policy_utility": round(utility, 6), "policy_learning": round(adaptive, 6),
                "policy_continuation": round(continuation, 6), "policy_score": candidate.score, **extra})
            candidate.reasons.insert(0, f"{style} policy: damage {vector['damage']:.0%}, KO {vector['ko']:.0%}, risk {vector['faint_risk']:.0%}.")
        return sorted(candidates, key=lambda c: (-c.score, c.terastallize, c.label))

    def features(self, battle, candidate, engine, profile, roles, endangered=0.):
        vector = {key: 0. for key in WEIGHTS}
        diagnostics = candidate.diagnostics
        own, foe = battle.active_pokemon, battle.opponent_active_pokemon
        switching = isinstance(candidate.action, Pokemon)
        actor = candidate.action if switching else own
        forced = bool(getattr(battle, "force_switch", False))
        if actor is None or foe is None:
            vector["damage"] = unit(diagnostics.get("expected_dealt"))
            return vector, {}
        move = None if switching else candidate.action
        vector.update(damage=unit(diagnostics.get("expected_dealt")), ko=unit(diagnostics.get("ko_chance")),
                      incoming=unit(diagnostics.get("expected_received")), faint_risk=unit(diagnostics.get("faint_risk")),
                      switch=float(switching and not forced), tera=float(candidate.terastallize))
        # Positionally costly setup/switch replies must not hide a lethal direct
        # attack. Mix the response-weighted forecast with the worst reply's mean
        # over chance (not its luckiest crit). HO can still accept more risk.
        robust = .35 if profile.get("archetype") == "hyper_offense" else .5
        for feature, diagnostic in (("incoming", "search_received_worst"),
                                    ("faint_risk", "search_faint_risk_worst")):
            if diagnostic in diagnostics:
                vector[feature] += robust * max(0., unit(diagnostics[diagnostic]) - vector[feature])
        threats = engine._threats(foe, actor, battle, target_tera=candidate.terastallize, switched=switching)
        first = sum(weight * engine._first(actor, move, foe, other, battle) for other, weight, _ in threats) if move else 0.
        ties = sum(weight for other, weight, _ in threats if move and engine._first(actor, move, foe, other, battle) == .5)
        survival = 1 - vector["faint_risk"]
        role = roles.get(base_species_id(actor.species), set())
        if "suicide_lead" in role and profile.get("archetype") == "hyper_offense":
            # The lead may trade itself only for actual field progress.
            expendable = sum(not mon.fainted for mon in battle.team.values()) >= 4
        else:
            expendable = False
        if switching:
            entry = min(_hp(actor), engine._hazards(actor, battle))
            if not diagnostics.get("search_legal"):
                # The emergency enumerator includes entry damage in received;
                # official search reports direct damage separately.
                vector["incoming"] = max(0., vector["incoming"] - entry)
            vector["resource"] += entry
            pressure, risk = self.next_matchup(battle, actor, foe, engine, max(0., _hp(actor) - entry - vector["incoming"]), switched=True)
            vector["matchup"] = pressure * survival - risk * .6
            if forced:
                vector["matchup"] = pressure - risk
                vector["incoming"] = entry
            if own and not own.fainted:
                if own.ability == "regenerator":
                    vector["heal"] += min(1 / 3, 1 - _hp(own))
                if own.ability == "naturalcure" and own.status:
                    vector["heal"] += .12
                boosts = sum(max(0, own.boosts.get(stat, 0)) for stat in ("atk", "spa", "spe"))
                vector["resource"] += min(.45, boosts * .045)
                is_condition = base_species_id(own.species) in {base_species_id(s) for s in profile.get("win_conditions", [])}
                vector["preserve"] = endangered * survival * (1. if is_condition else .45)
            vector["weather"] = self.weather_value(battle, actor, None, profile, switching=True) * survival
        else:
            can_act = sum(weight * (1 - (1 - engine._first(actor, move, foe, other, battle)) * damage.ko_chance(_hp(actor)))
                          for other, weight, damage in threats) if threats else 1.
            # Simulator probabilities account for flinch, status and exact move
            # order. Utility moves only receive benefits when they can resolve.
            accuracy = engine._accuracy(move, engine._snapshot(actor), engine._snapshot(foe), battle)
            resolution = unit(can_act * accuracy)
            vector["heal"] = self.heal_value(battle, actor, foe, move, engine, threats) * resolution
            vector["setup"] = self.setup_value(battle, actor, foe, move, engine, threats, survival) * resolution
            hazard_value = self.hazard_value(battle, actor, foe, move, engine)
            if hazard_value > 0 and "search_hazard_retention" in diagnostics:
                # This is the unconditional probability of the new layer
                # remaining after both actions, including paralysis, misses,
                # removal and pivots. Do not apply action resolution twice.
                vector["hazards"] = hazard_value * unit(diagnostics["search_hazard_retention"])
            else:
                retention = (self.hazard_retention(battle, actor, foe, move, engine, candidate.terastallize)
                             if hazard_value > 0 else 1.)
                vector["hazards"] = hazard_value * resolution * retention
            vector["removal"] = self.removal_value(battle, actor, foe, move, engine) * resolution
            vector["status"] = self.status_value(battle, actor, foe, move, engine) * resolution
            vector["weather"] = self.weather_value(battle, actor, move, profile) * resolution
            vector["tempo"] = (first * min(1., vector["damage"] + vector["ko"]) +
                               (.5 if move.self_switch and battle.available_switches and vector["damage"] > 0 else 0))
            # Direct damage is measured against the target's max HP. Drain and
            # recoil consume/restore the user's HP, so convert that denominator.
            foe_snapshot = engine._snapshot(foe)
            damage_in_own_hp = vector["damage"] * foe_snapshot.max_hp / max(1, actor.max_hp)
            if move.entry.get("struggleRecoil"):
                # Struggle costs a quarter of the user's HP, bypassing both
                # Magic Guard and Rock Head, independently of damage dealt.
                vector["resource"] += .25 * resolution
            elif move.recoil and actor.ability not in {"rockhead", "magicguard"}:
                vector["resource"] += damage_in_own_hp * move.recoil
            if move.self_destruct:
                vector["resource"] += _hp(actor) * (0.3 if expendable else 1.)
            if move.drain:
                drained = damage_in_own_hp * move.drain
                if foe_snapshot.ability == "liquidooze":
                    if actor.ability != "magicguard":
                        vector["resource"] += drained
                else:
                    vector["heal"] += min(1 - _hp(actor), drained)
            elif move.id == "strengthsap" and foe_snapshot.ability == "liquidooze" and actor.ability != "magicguard":
                vector["resource"] += foe_snapshot.stats["atk"] / max(1, actor.max_hp) * resolution
            if move.entry.get("recharge"):
                vector["resource"] += .5
            if "charge" in move.flags and actor.item != "powerherb" and not (
                move.id in {"solarbeam", "solarblade"} and "SUNNYDAY" in _names(battle.weather)):
                vector["resource"] += .55
            vector["residual"] = self.protect_value(actor, foe, move) * resolution
            vector["denial"] = self.denial_value(actor, foe, move, battle) * resolution
            if expendable and (vector["hazards"] > .1 or vector["denial"] > .3):
                vector["faint_risk"] *= .65
        # Public outcome variance discounts brittle lines; bounded so a risky
        # necessary win condition is still available when all safe lines lose.
        vector["uncertainty"] = min(1., finite(diagnostics.get("search_damage_std")) +
                                    finite(diagnostics.get("search_received_std")))
        return {key: max(-1., min(1., finite(value))) for key, value in vector.items()}, {
            "policy_first_probability": round(first, 6), "policy_speed_tie_probability": round(ties, 6)}

    @staticmethod
    def entry_first(battle, actor, move, foe, other, engine, remaining):
        """Initiative after entry, without changing live or cached Pokémon."""
        entry = engine._snapshot(actor, switched=True)
        stage = 0
        if ("STICKY_WEB" in _names(battle.side_conditions)
                and entry.item not in {"heavydutyboots", "clearamulet"}
                and entry.ability not in {"clearbody", "whitesmoke", "fullmetalbody", "mirrorarmor"}
                and _Arena(battle, entry, foe, True).is_grounded(entry)):
            stage = 1 if entry.ability == "contrary" else -2 if entry.ability == "simple" else -1
        priority = engine._priority(actor, move)
        if entry.ability == "galewings" and move.type == PokemonType.FLYING and _hp(actor) >= .999 and remaining < .999:
            priority -= 1
        priority -= engine._priority(foe, other)
        if priority:
            return float(priority > 0)
        # Keep the original identity so _speed reads our Tailwind, not theirs.
        speed = engine._speed(actor, battle) / _stage(actor.boosts.get("spe", 0)) * _stage(stage)
        foe_speed = engine._speed(foe, battle)
        if speed == foe_speed:
            return .5
        first = speed > foe_speed
        if Field.TRICK_ROOM in getattr(battle, "fields", {}):
            first = not first
        return float(first)

    @staticmethod
    def next_matchup(battle, actor, foe, engine, remaining, switched=False):
        if remaining <= 0:
            return 0., 1.
        threats = engine._threats(foe, actor, battle, switched=switched)
        values = []
        for move, _ in engine._moves(actor):
            if move.category == MoveCategory.STATUS:
                continue
            outgoing = engine._damage(actor, foe, move, battle, switched_attacker=switched)
            pressure = danger = 0.
            for other, probability, incoming in threats:
                first = (BattlePolicy.entry_first(battle, actor, move, foe, other, engine, remaining)
                         if switched else engine._first(actor, move, foe, other, battle))
                ko = outgoing.ko_chance(_hp(foe))
                can_act = 1 - (1 - first) * incoming.ko_chance(remaining)
                pressure += probability * (min(_hp(foe), outgoing.expected) + .5 * ko) * can_act
                danger += probability * incoming.ko_chance(remaining) * (1 - first * ko)
            values.append((pressure - danger, pressure, danger))
        best = max(values, default=(0., 0., 0.))
        return min(1., best[1]), min(1., best[2])

    @staticmethod
    def heal_value(battle, actor, foe, move, engine, threats):
        hp = _hp(actor)
        amount = move.heal
        if move.id == "rest":
            if actor.status == Status.SLP or actor.ability in {"insomnia", "vitalspirit"}:
                return 0.
            amount = 1.
        if move.id == "strengthsap":
            if engine._snapshot(foe).ability == "liquidooze":
                return 0.
            amount = engine._snapshot(foe).stats["atk"] / max(1, actor.max_hp)
        if move.id == "wish":
            return min(.3, max(0., .75 - hp))
        if not amount:
            return 0.
        if move.id in {"synthesis", "moonlight", "morningsun"}:
            climate = _names(battle.weather)
            amount = 2 / 3 if "SUNNYDAY" in climate else .25 if climate else .5
        if move.id == "shoreup" and "SANDSTORM" in _names(battle.weather):
            amount = 2 / 3
        # Slow recovery may restore damage received that turn, but cannot heal
        # a Pokémon that is already knocked out before it acts.
        useful = sum(weight * min(amount, max(0., 1 - hp + (1 - engine._first(actor, move, foe, other, battle)) * incoming.expected))
                     for other, weight, incoming in threats) if threats else min(amount, 1 - hp)
        if move.id == "rest" and actor.item not in {"chestoberry", "lumberry"} and "sleeptalk" not in actor.moves:
            useful *= .65
        return useful

    @staticmethod
    def setup_value(battle, actor, foe, move, engine, threats, survival):
        if move.id not in SETUP:
            return 0.
        boosts = move.boosts if getattr(move.target, "name", "") == "SELF" else move.self_boost
        if not boosts:
            return 0.
        active_moves = {m.id for m in foe.moves.values() if m.current_pp > 0}
        counters = PHASING.copy()
        if sum(not mon.fainted for mon in battle.team.values()) <= 1:
            counters -= {"roar", "whirlwind", "dragontail", "circlethrow"}
        if "TAUNT" in _names(foe.effects):
            counters &= {"dragontail", "circlethrow", "clearsmog"}
        if foe.status in {Status.SLP, Status.FRZ}:
            counters.clear()
        from .gameplan import likely_ability
        unaware = likely_ability(foe, "unaware") and actor.ability not in {"moldbreaker", "teravolt", "turboblaze"}
        if active_moves & counters or unaware:
            return 0.
        incoming = sum(weight * damage.expected for _, weight, damage in threats)
        safety = max(0., min(1., (_hp(actor) - 1.25 * incoming) / max(.15, _hp(actor)))) * survival
        categories = {slot.category for slot in actor.moves.values() if slot.current_pp > 0}
        gain = 0.
        for stat, change in boosts.items():
            if change <= 0:
                continue
            if stat == "atk" and MoveCategory.PHYSICAL not in categories or stat == "spa" and MoveCategory.SPECIAL not in categories:
                continue
            gain += min(change, 6 - actor.boosts.get(stat, 0)) * {"atk": .27, "spa": .27, "spe": .25, "def": .13, "spd": .13}.get(stat, 0)
        coverage = max((engine._damage(actor, foe, m, battle).expected for m in actor.moves.values() if m.category != MoveCategory.STATUS), default=0.)
        return min(1., gain) * safety * min(1., coverage / .18)

    @staticmethod
    def hazard_value(battle, actor, foe, move, engine):
        hazard = (getattr(move.side_condition, "name", "") or {"stoneaxe": "STEALTH_ROCK", "ceaselessedge": "SPIKES"}.get(move.id, ""))
        if hazard not in {"STEALTH_ROCK", "SPIKES", "TOXIC_SPIKES", "STICKY_WEB"}:
            return 0.
        if foe.ability == "magicbounce" and move.category == MoveCategory.STATUS and actor.ability != "moldbreaker":
            return -.35
        existing = {getattr(k, "name", str(k)): v for k, v in battle.opponent_side_conditions.items()}
        layers = finite(existing.get(hazard))
        if layers >= {"SPIKES": 3, "TOXIC_SPIKES": 2}.get(hazard, 1):
            return 0.
        arena = _Arena(battle, actor, foe, True)
        total = 0.
        for target in _opponents(battle):
            if target.fainted or target is foe:
                continue
            target = engine._snapshot(target)
            if target.item == "heavydutyboots":
                continue
            grounded = arena.is_grounded(target)
            if hazard == "STEALTH_ROCK" and target.ability != "magicguard":
                total += .125 * PokemonType.ROCK.damage_multiplier(target.type_1, target.type_2, type_chart=engine_type_chart(target))
            elif hazard == "SPIKES" and grounded and target.ability != "magicguard":
                total += (.125, 1 / 24, 1 / 12)[int(layers)]
            elif hazard == "TOXIC_SPIKES" and grounded and not target.status and not set(target.types) & {PokemonType.STEEL, PokemonType.POISON} and target.ability not in {"magicguard", "poisonheal", "immunity", "purifyingsalt"}:
                total += .12 if layers == 0 else .04
            elif hazard == "STICKY_WEB" and grounded and target.ability not in {"clearbody", "whitesmoke", "fullmetalbody", "contrary"} and target.item != "clearamulet":
                speed = engine._speed(target, battle)
                allies = [p for p in battle.team.values() if not p.fainted and p is not actor]
                flips = sum(speed * 2 / 3 < engine._speed(p, battle) <= speed for p in allies)
                total += .12 * flips / max(1, len(allies))
        return min(.8, total)

    @staticmethod
    def hazard_retention(battle, actor, foe, move, engine, tera=False):
        """Fallback for a revealed remover acting after our setter this turn.

        A known counter is a possible response, not a certain action. Mix its
        public move prior with the same bounded risk preference as damage.
        Exact simulated retention replaces this estimate when available.
        """
        expected = worst = 0.
        for other, weight, _ in engine._threats(foe, actor, battle, target_tera=tera):
            if other.id not in REMOVAL or other.id not in foe.moves or other.current_pp <= 0:
                continue
            if other.category == MoveCategory.STATUS and "TAUNT" in _names(foe.effects):
                continue
            if other.id in {"rapidspin", "mortalspin"} and engine._damage(
                    foe, actor, other, battle, tera_defender=tera).high <= 0:
                continue
            if other.id == "defog" and actor.ability in {"goodasgold", "magicbounce"}:
                continue
            first = engine._first(actor, move, foe, other, battle)
            survives = 1 - engine._damage(actor, foe, move, battle).ko_chance(_hp(foe))
            counter = first * survives * engine._accuracy(other, engine._snapshot(foe), engine._snapshot(actor), battle)
            expected += weight * counter
            worst = max(worst, counter)
        risk = unit(getattr(engine, "risk", .35))
        return unit(1 - (1 - risk) * expected - risk * worst)

    @staticmethod
    def removal_value(battle, actor, foe, move, engine):
        if move.id not in REMOVAL:
            return 0.
        if move.id in {"rapidspin", "mortalspin"} and engine._damage(actor, foe, move, battle).high <= 0:
            return 0.
        if move.id == "defog" and foe.ability in {"goodasgold", "magicbounce"}:
            return 0.
        total = sum(engine._hazards(mon, battle) for mon in battle.team.values() if not mon.fainted)
        own_conditions = _names(battle.side_conditions)
        if "TOXIC_SPIKES" in own_conditions:
            total += .1 * sum(not mon.fainted and not mon.status and mon.item != "heavydutyboots" and
                             not set(mon.types) & {PokemonType.FLYING, PokemonType.POISON, PokemonType.STEEL}
                             and mon.ability not in {"levitate", "magicguard", "poisonheal"} for mon in battle.team.values())
        if "STICKY_WEB" in own_conditions:
            total += .08 * sum(not mon.fainted and mon.item != "heavydutyboots" and PokemonType.FLYING not in mon.types for mon in battle.team.values())
        if move.id == "defog":
            total -= .5 * sum(engine._hazards(mon, battle, own=False) for mon in _opponents(battle) if not mon.fainted)
        return min(.9, total)

    @staticmethod
    def status_value(battle, actor, foe, move, engine):
        value = engine._status_value(move, actor, foe, battle) / 40
        for secondary in move.secondary:
            if secondary.get("status"):
                value += engine._status_value(move, actor, foe, battle, Status[secondary["status"].upper()]) / 40 * secondary.get("chance", 100) / 100
        return max(-1., min(1., value))

    @staticmethod
    def weather_value(battle, actor, move, profile, switching=False):
        climate = profile.get("weather", "")
        if not climate:
            return 0.
        current = {CLIMATE_NAMES.get(name) for name in _names(battle.weather)}
        requested = WEATHER_SETTERS.get(actor.ability) if switching else WEATHER_SETTERS.get(move.id if move else "")
        if requested != climate or climate in current:
            return 0.
        abusers = {base_species_id(s) for s in profile.get("weather_abusers", [])}
        alive = [mon for mon in battle.team.values() if not mon.fainted and base_species_id(mon.species) in abusers]
        if any(mon.ability in {"cloudnine", "airlock"} for mon in (battle.active_pokemon, battle.opponent_active_pokemon) if mon):
            return 0.
        return min(.7, len(alive) * .18)

    @staticmethod
    def protect_value(actor, foe, move):
        if not move.is_protect_move:
            return 0.
        if actor.protect_counter:
            return -.1 * min(3, actor.protect_counter)
        own_heal = min(1 - _hp(actor), .0625) if actor.item == "leftovers" else 0.
        foe_chip = 0. if foe.ability in {"magicguard", "poisonheal"} else .0625 if foe.status == Status.BRN else .125 if foe.status in {Status.PSN, Status.TOX} else 0.
        own_chip = 0. if actor.ability in {"magicguard", "poisonheal"} else .0625 if actor.status == Status.BRN else .125 if actor.status in {Status.PSN, Status.TOX} else 0.
        return own_heal + foe_chip - own_chip

    @staticmethod
    def denial_value(actor, foe, move, battle):
        if move.id in {"haze", "clearsmog", "roar", "whirlwind", "dragontail", "circlethrow"}:
            if move.id in {"roar", "whirlwind", "dragontail", "circlethrow"} and len([p for p in _opponents(battle) if not p.fainted]) <= 1:
                return 0.
            return min(1., .15 * sum(max(0, foe.boosts.get(stat, 0)) for stat in ("atk", "spa", "spe")))
        if move.id in {"taunt", "encore"} and foe.ability not in {"goodasgold", "magicbounce", "aromaveil"}:
            if move.id == "encore":
                return .5 if foe.last_move and foe.last_move.category == MoveCategory.STATUS else 0.
            return min(.6, .15 * sum(slot.category == MoveCategory.STATUS for slot in foe.moves.values()))
        if move.id in SCREENS:
            if move.id == "auroraveil" and not _names(battle.weather) & {"SNOW", "SNOWSCAPE", "HAIL"}:
                return 0.
            if move.id.upper() in {name.replace("_", "") for name in _names(battle.side_conditions)}:
                return 0.
            return min(.7, .12 * sum(not mon.fainted for mon in battle.team.values()))
        return 0.


def engine_type_chart(mon):
    from poke_env.data import GenData
    return GenData.from_gen(mon.gen).type_chart
