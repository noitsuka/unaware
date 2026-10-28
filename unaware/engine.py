"""Legal action enumeration and emergency heuristic for Gen 9 singles.

Damage uses poke-env's Gen 9 calculator on private copies. Unknown EVs/nature,
item and ability use the most frequent available Smogon marginal (not a joint
set); unknown moves use usage priors plus revealed information. With no stats
cache, conservative STAB and learnset guesses are used. These are estimates,
never discoveries about an opponent's hidden team. No network is used here.

The live runner re-ranks these candidates with the official simulator through
SimulatorSearch. This module supplies estimates for ordering, diagnostics and
fallback if that worker fails; its scores do not override successful search.
Neither evaluator guarantees optimal play against hidden sets and randomness.
"""

from __future__ import annotations

import copy
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from poke_env.battle import Field, Move, MoveCategory, Pokemon, PokemonType, Status
from poke_env.calc.damage_calc_gen9 import calculate_damage
from poke_env.data import GenData, to_id_str
from poke_env.stats import compute_raw_stats

LOG = logging.getLogger(__name__)
STATS = ("hp", "atk", "def", "spa", "spd", "spe")
FEATURE_NAMES = ("damage", "ko", "incoming", "faint_risk", "heal", "setup",
                 "hazards", "removal", "status", "pivot", "switch", "tera",
                 "accuracy", "preserve")
HAZARDS = {"STEALTH_ROCK", "SPIKES", "TOXIC_SPIKES", "STICKY_WEB"}
FALLBACK_STAB = {
    "NORMAL": ("bodyslam", "hypervoice"), "FIRE": ("flareblitz", "flamethrower"),
    "WATER": ("liquidation", "surf"), "ELECTRIC": ("wildcharge", "thunderbolt"),
    "GRASS": ("leafblade", "energyball"), "ICE": ("iciclecrash", "icebeam"),
    "FIGHTING": ("closecombat", "aurasphere"), "POISON": ("poisonjab", "sludgebomb"),
    "GROUND": ("earthquake", "earthpower"), "FLYING": ("bravebird", "airslash"),
    "PSYCHIC": ("psychicfangs", "psychic"), "BUG": ("xscissor", "bugbuzz"),
    "ROCK": ("stoneedge", "powergem"), "GHOST": ("shadowclaw", "shadowball"),
    "DRAGON": ("dragonclaw", "dragonpulse"), "DARK": ("knockoff", "darkpulse"),
    "STEEL": ("ironhead", "flashcannon"), "FAIRY": ("playrough", "moonblast"),
}

KNOCK_OFF_TARGETS = {"leftovers", "blacksludge", "eviolite", "heavydutyboots", "assaultvest",
                     "toxicorb", "flameorb", "choiceband", "choicespecs", "choicescarf", "boosterenergy"}


def _names(mapping: Any) -> set[str]:
    return {getattr(k, "name", str(k)).upper() for k in (mapping or {})}


def _hp(mon: Pokemon | None) -> float:
    if mon is None or mon.fainted:
        return 0.0
    return min(1.0, mon.current_hp_fraction) if mon.max_hp else 1.0


def _stage(value: int) -> float:
    return (2 + value) / 2 if value >= 0 else 2 / (2 - value)


def _opponents(battle: Any) -> list[Pokemon]:
    """Keep preview information after poke-env switches to its revealed roster."""
    revealed = list(getattr(battle, "opponent_team", {}).values())
    def family(mon: Pokemon) -> str:
        entry = GenData.from_gen(mon.gen).pokedex.get(mon.species, {})
        return to_id_str(entry.get("baseSpecies", mon.species))
    seen = {family(p) for p in revealed}
    return revealed + [p for p in getattr(battle, "teampreview_opponent_team", []) if family(p) not in seen]


@dataclass
class Candidate:
    action: Move | Pokemon
    terastallize: bool = False
    score: float = 0.0
    label: str = ""
    reasons: list[str] = field(default_factory=list)
    features: dict[str, float] = field(default_factory=dict)
    diagnostics: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        """Stable numeric contract for bounded, externally applied adaptation.

        Features describe the chosen action, not the observed result. Damage and
        incoming are fractions of each affected Pokémon's full HP, never team HP.
        Diagnostics are estimates before executing the action. They cannot prove
        that a particular action caused a loss. ``base_score`` stays unchanged
        if a caller later adjusts ``score`` using locally learned preferences.
        """
        self.features = {name: max(0.0, min(1.0, float(self.features.get(name, 0.0))))
                         for name in FEATURE_NAMES}
        defaults = {"expected_received": 0.0, "expected_dealt": 0.0,
                    "ko_chance": 0.0, "faint_risk": 0.0,
                    "damage_low": 0.0, "damage_high": 0.0, "accuracy": 0.0,
                    "base_score": float(self.score), "has_damage_estimate": 0.0}
        defaults.update(self.diagnostics)
        self.diagnostics = defaults


@dataclass(frozen=True)
class Damage:
    low: float
    high: float
    accuracy: float
    outcomes: tuple = ()

    @property
    def expected(self) -> float:
        if self.outcomes:
            return sum((low + high) * 0.5 * weight for low, high, weight in self.outcomes) * self.accuracy
        return (self.low + self.high) * 0.5 * self.accuracy

    def ko_chance(self, hp: float) -> float:
        if self.outcomes:
            return sum(weight * Damage(low, high, self.accuracy).ko_chance(hp)
                       for low, high, weight in self.outcomes)
        if self.high < hp or self.high <= 0:
            return 0.0
        if self.low >= hp:
            return self.accuracy
        return self.accuracy * min(1.0, (self.high - hp) / max(0.00001, self.high - self.low))


class _Arena:
    """Minimal read-only battle view accepted by the upstream damage calculator."""

    def __init__(self, battle: Any, attacker: Pokemon, defender: Pokemon, own_attack: bool):
        self.gen = getattr(battle, "gen", 9)
        self.player_role, self.opponent_role = "p1", "p2"
        self.weather = getattr(battle, "weather", {})
        self.fields = getattr(battle, "fields", {})
        self.side_conditions = getattr(battle, "side_conditions", {})
        self.opponent_side_conditions = getattr(battle, "opponent_side_conditions", {})
        self.team = getattr(battle, "team", {})
        self.opponent_team = getattr(battle, "opponent_team", {})
        self.active_pokemon = attacker if own_attack else defender
        self.opponent_active_pokemon = defender if own_attack else attacker
        self.all_active_pokemons = [self.active_pokemon, self.opponent_active_pokemon]

    def get_pokemon(self, identifier: str) -> Pokemon:
        return self.active_pokemon if identifier.startswith("p1") else self.opponent_active_pokemon

    def is_grounded(self, mon: Pokemon) -> bool:
        if Field.GRAVITY in self.fields or mon.item == "ironball":
            return True
        return not (PokemonType.FLYING in mon.types or mon.ability == "levitate"
                    or mon.item == "airballoon" or "MAGNET_RISE" in _names(mon.effects))


class _SingleHitMove(Move):
    @property
    def n_hit(self):
        return (1, 1)


class DecisionEngine:
    def __init__(self, meta: Any = None, risk: float = 0.35, allow_tera: bool = True):
        self.meta = meta
        self.risk = max(0.0, min(1.0, float(risk)))
        self.allow_tera = allow_tera
        self._copies: dict[tuple, Pokemon] = {}
        self._damages: dict[tuple, Damage] = {}
        self._meta_cache: dict[str, dict] = {}
        self._warnings: set[str] = set()
        self._last_switch: dict[str, tuple[int, str]] = {}

    def _reset(self):
        self._copies.clear()
        self._damages.clear()
        self._meta_cache.clear()

    def _usage(self, mon: Pokemon) -> dict:
        if mon.species not in self._meta_cache:
            self._meta_cache[mon.species] = self.meta.species(mon.species) if self.meta else {}
        return self._meta_cache[mon.species] or {}

    @staticmethod
    def _most_common(data: dict) -> str | None:
        valid = [(k, v) for k, v in data.items() if isinstance(v, (int, float)) and v > 0]
        return max(valid, key=lambda pair: pair[1])[0] if valid else None

    def _snapshot(self, mon: Pokemon, tera: bool = False, switched: bool = False) -> Pokemon:
        key = (id(mon), tera, switched)
        if key in self._copies:
            return self._copies[key]
        result = copy.copy(mon)
        result._boosts = dict(mon.boosts)
        result._effects = dict(mon.effects)
        result._stats = dict(mon.stats)
        usage = self._usage(mon)
        if any(result.stats.get(k) is None for k in STATS):
            evs, nature = mon.evs, mon.nature
            spread = self._most_common(usage.get("Spreads", {}))
            if evs is None and spread:
                try:
                    nature, raw = spread.split(":", 1)
                    evs = [int(x) for x in raw.split("/")]
                    if len(evs) != 6 or any(x < 0 or x > 252 for x in evs):
                        evs = None
                except (ValueError, TypeError):
                    evs = None
            if evs is None:
                # A neutral, legal spread; no claim that hidden stats are known.
                evs = [84, 84, 84, 84, 84, 80]
            data = GenData.from_gen(mon.gen)
            nature = to_id_str(nature or "Serious")
            if nature not in data.natures:
                nature = "serious"
            calculated = compute_raw_stats(mon.species, evs, mon.ivs or [31] * 6,
                                           mon.level, nature, data)
            for stat, value in zip(STATS, calculated):
                if result.stats.get(stat) is None:
                    result._stats[stat] = value
        result._max_hp = int(result.stats["hp"])
        result._current_hp = max(1, round(result.max_hp * _hp(mon))) if not mon.fainted else 0
        if result.ability is None:
            result._ability = to_id_str(self._most_common(usage.get("Abilities", {})) or
                                        (mon.possible_abilities[0] if mon.possible_abilities else ""))
        if result.item == GenData.from_gen(mon.gen).UNKNOWN_ITEM:
            result._item = to_id_str(self._most_common(usage.get("Items", {})) or "")
        if tera and result.tera_type:
            result._terastallized = True
        if switched:
            result._boosts = {k: 0 for k in result.boosts}
            result._effects = {}
            result._active_turns = 1
        self._copies[key] = result
        return result

    def _moves(self, mon: Pokemon, opponent: bool = False) -> list[tuple[Move, float]]:
        revealed = {m.id: (m, 1.0) for m in mon.moves.values() if m.current_pp > 0}
        if not opponent or len(mon.moves) >= 4:
            return list(revealed.values())
        choices = dict(revealed)
        entries = sorted(self._usage(mon).get("Moves", {}).items(),
                         key=lambda kv: float(kv[1] or 0), reverse=True)
        largest = max((float(v) for _, v in entries), default=1.0) or 1.0
        for name, frequency in entries[:12]:
            move_id = to_id_str(name)
            if move_id not in choices and move_id in GenData.from_gen(mon.gen).moves:
                choices[move_id] = (Move(move_id, gen=mon.gen), min(0.85, float(frequency) / largest))
            if len(choices) >= 8:
                break
        if not entries and len(mon.moves) < 4:
            physical = mon.base_stats["atk"] >= mon.base_stats["spa"]
            for type_ in mon.types:
                pair = FALLBACK_STAB.get(type_.name)
                if pair:
                    move_id = pair[0 if physical else 1]
                    choices.setdefault(move_id, (Move(move_id, gen=mon.gen), 0.35))
            # Coverage is guessed only if the species actually learns the move.
            for move_id in ("earthquake", "closecombat", "icebeam", "flamethrower", "thunderbolt"):
                if move_id in mon.learnset and len(choices) < 5:
                    choices.setdefault(move_id, (Move(move_id, gen=mon.gen), 0.12))
        return list(choices.values())

    def _accuracy(self, move: Move, attacker: Pokemon, defender: Pokemon, battle: Any) -> float:
        acc = move.accuracy
        guaranteed = attacker.ability == "noguard" or defender.ability == "noguard" or move.entry.get("accuracy") is True
        weather = _names(getattr(battle, "weather", {}))
        if move.id in {"thunder", "hurricane"}:
            if "RAINDANCE" in weather:
                guaranteed = True
            if "SUNNYDAY" in weather:
                acc = 0.5
        if move.id == "blizzard" and weather & {"SNOW", "HAIL"}:
            guaranteed = True
        boost = max(-6, min(6, attacker.boosts.get("accuracy", 0) - defender.boosts.get("evasion", 0)))
        acc *= (3 + boost) / 3 if boost >= 0 else 3 / (3 - boost)
        if attacker.ability == "compoundeyes":
            acc *= 1.3
        if attacker.ability == "hustle" and move.category == MoveCategory.PHYSICAL:
            acc *= 0.8
        if attacker.item == "widelens":
            acc *= 1.1
        if Field.GRAVITY in getattr(battle, "fields", {}):
            acc *= 5 / 3
        acc = 1.0 if guaranteed else max(0.0, min(1.0, acc))
        if attacker.status == Status.PAR:
            acc *= 0.75
        if attacker.status == Status.SLP and not move.sleep_usable:
            acc *= 0.35  # Sleep duration is hidden; waking this turn is uncertain.
        if attacker.status == Status.FRZ:
            acc *= 1.0 if move.entry.get("thawsUser") else 0.2
        return max(0.0, min(1.0, acc))

    def _damage(self, attacker: Pokemon, defender: Pokemon, move: Move, battle: Any,
                tera_attacker: bool = False, tera_defender: bool = False,
                switched_attacker: bool = False, switched_defender: bool = False) -> Damage:
        key = (id(attacker), id(defender), move.id, tera_attacker, tera_defender,
               switched_attacker, switched_defender)
        if key in self._damages:
            return self._damages[key]
        atk = self._snapshot(attacker, tera_attacker, switched_attacker)
        dfn = self._snapshot(defender, tera_defender, switched_defender)
        own = attacker is getattr(battle, "active_pokemon", None) or any(
            attacker is p for p in getattr(battle, "team", {}).values())
        if switched_attacker:
            atk = copy.copy(atk)
            atk._current_hp = max(1, round(atk.max_hp * max(0, _hp(attacker) - self._hazards(attacker, battle, own))))
        if switched_defender:
            dfn = copy.copy(dfn)
            dfn._current_hp = max(1, round(dfn.max_hp * max(0, _hp(defender) - self._hazards(defender, battle, not own))))
            if dfn.ability == "intimidate" and atk.ability not in {
                "clearbody", "whitesmoke", "fullmetalbody", "innerfocus", "oblivious", "owntempo", "scrappy", "guarddog"
            } and atk.item != "clearamulet":
                atk = copy.copy(atk)
                atk._boosts = dict(atk.boosts)
                atk._boosts["atk"] = max(-6, atk.boosts["atk"] - 1)
        arena = _Arena(battle, atk, dfn, own)
        if (atk.ability in {"moldbreaker", "teravolt", "turboblaze"} or move.ignore_ability) and dfn.item != "abilityshield":
            # Also corrects the upstream calculator's special-case Levitate check.
            dfn = copy.copy(dfn)
            dfn._ability = ""
            arena = _Arena(battle, atk, dfn, own)
        outcomes = ()
        try:
            low, high = calculate_damage("p1" if own else "p2", "p2" if own else "p1", move, arena)
            intact_disguise = dfn.ability == "disguise" and "busted" not in dfn.species
            if move.category != MoveCategory.STATUS and (move.n_hit != (1, 1) or intact_disguise) and high > 0:
                minimum, maximum = move.n_hit
                if atk.ability == "skilllink":
                    minimum = maximum
                if atk.item == "loadeddice" and (minimum, maximum) == (2, 5):
                    minimum = 4
                counts = list(range(minimum, maximum + 1))
                weights = [0.35, 0.35, 0.15, 0.15] if counts == [2, 3, 4, 5] else [1 / len(counts)] * len(counts)
                single = _SingleHitMove(move.id, gen=move.gen)
                hit_ranges = []
                target = copy.copy(dfn)
                target._boosts = dict(dfn.boosts)
                for hit in range(maximum):
                    if hit == 0 and intact_disguise:
                        part_low = part_high = math.floor(target.max_hp / 8)
                        target._ability = ""
                    else:
                        one_arena = _Arena(battle, atk, target, own)
                        part_low, part_high = calculate_damage("p1" if own else "p2", "p2" if own else "p1", single, one_arena)
                    hit_ranges.append((part_low, part_high))
                    if dfn.ability == "stamina" and move.category == MoveCategory.PHYSICAL:
                        target._boosts["def"] = min(6, target.boosts["def"] + 1)
                ranges = [(sum(x[0] for x in hit_ranges[:count]), sum(x[1] for x in hit_ranges[:count]), weight)
                          for count, weight in zip(counts, weights)]
                low, high = ranges[0][0], ranges[-1][1]
                outcomes = tuple((a / dfn.max_hp, b / dfn.max_hp, weight) for a, b, weight in ranges)
        except (ValueError, KeyError, TypeError, AssertionError, ZeroDivisionError) as exc:
            if move.id not in self._warnings:
                LOG.warning("Approximate fallback for %s: %s", move.id, exc)
                self._warnings.add(move.id)
            multiplier = move.type.damage_multiplier(dfn.type_1, dfn.type_2,
                              type_chart=GenData.from_gen(atk.gen).type_chart)
            a, d = ("atk", "def") if move.category == MoveCategory.PHYSICAL else ("spa", "spd")
            high = ((2 * atk.level / 5 + 2) * move.base_power * atk.stats[a] * _stage(atk.boosts[a]) /
                    max(1, dfn.stats[d] * _stage(dfn.boosts[d])) / 50 + 2) * multiplier
            high *= 1.5 if move.type in atk.types else 1.0
            low = high * 0.85
            if move.category == MoveCategory.STATUS:
                low = high = 0
        if move.id == "fakeout" and not atk.first_turn:
            low = high = 0
        if move.id in {"superfang", "naturesmadness", "ruination"} and high > 0:
            low = high = dfn.current_hp / 2
        if (dfn.item == "focussash" or dfn.ability == "sturdy") and _hp(dfn) >= 0.999 and move.n_hit == (1, 1):
            low, high = min(low, dfn.current_hp - 1), min(high, dfn.current_hp - 1)
        damage = Damage(max(0, low) / dfn.max_hp, max(0, high) / dfn.max_hp,
                        self._accuracy(move, atk, dfn, battle), outcomes)
        self._damages[key] = damage
        return damage

    def _speed(self, mon: Pokemon, battle: Any) -> float:
        pokemon = self._snapshot(mon)
        speed = pokemon.stats["spe"] * _stage(pokemon.boosts.get("spe", 0))
        if pokemon.item == "choicescarf":
            speed *= 1.5
        if pokemon.status == Status.PAR and pokemon.ability != "quickfeet":
            speed *= 0.5
        if pokemon.status and pokemon.ability == "quickfeet":
            speed *= 1.5
        weather = _names(getattr(battle, "weather", {}))
        doubles = {"swiftswim": "RAINDANCE", "chlorophyll": "SUNNYDAY", "sandrush": "SANDSTORM", "slushrush": "SNOW"}
        if doubles.get(pokemon.ability) in weather:
            speed *= 2
        if pokemon.ability == "surgesurfer" and Field.ELECTRIC_TERRAIN in getattr(battle, "fields", {}):
            speed *= 2
        own = mon is getattr(battle, "active_pokemon", None) or any(mon is p for p in getattr(battle, "team", {}).values())
        side = getattr(battle, "side_conditions" if own else "opponent_side_conditions", {})
        if "TAILWIND" in _names(side):
            speed *= 2
        return speed

    def _priority(self, mon: Pokemon, move: Move) -> int:
        ability = self._snapshot(mon).ability
        priority = move.priority
        if ability == "prankster" and move.category == MoveCategory.STATUS:
            priority += 1
        if ability == "triage" and "heal" in move.flags:
            priority += 3
        if ability == "galewings" and move.type == PokemonType.FLYING and _hp(mon) >= 0.999:
            priority += 1
        return priority

    def _first(self, mon: Pokemon, move: Move, foe: Pokemon, other: Move, battle: Any) -> float:
        priorities = self._priority(mon, move) - self._priority(foe, other)
        if priorities:
            return float(priorities > 0)
        speed, foe_speed = self._speed(mon, battle), self._speed(foe, battle)
        if speed == foe_speed:
            return 0.5
        first = speed > foe_speed
        if Field.TRICK_ROOM in getattr(battle, "fields", {}):
            first = not first
        return float(first)

    def _threats(self, foe: Pokemon, target: Pokemon, battle: Any,
                 target_tera: bool = False, switched: bool = False) -> list[tuple[Move, float, Damage]]:
        result = []
        for move, prior in self._moves(foe, opponent=True):
            damage = self._damage(foe, target, move, battle, tera_defender=target_tera, switched_defender=switched)
            usefulness = damage.expected + 0.5 * damage.ko_chance(_hp(target))
            if move.category == MoveCategory.STATUS:
                usefulness = 0.18 if move.status and not target.status else 0.07
            weight = max(0.005, prior) * math.exp(min(4.5, usefulness * 4))
            result.append((move, weight, damage))
        total = sum(w for _, w, _ in result) or 1
        return [(move, w / total, damage) for move, w, damage in result]

    def _hazards(self, mon: Pokemon, battle: Any, own: bool = True) -> float:
        pokemon = self._snapshot(mon, switched=True)
        if pokemon.item == "heavydutyboots" or pokemon.ability == "magicguard":
            return 0.0
        conditions = getattr(battle, "side_conditions" if own else "opponent_side_conditions", {})
        named = {getattr(k, "name", str(k)): v for k, v in conditions.items()}
        result = 0.0
        if "STEALTH_ROCK" in named:
            result += 0.125 * PokemonType.ROCK.damage_multiplier(pokemon.type_1, pokemon.type_2,
                           type_chart=GenData.from_gen(pokemon.gen).type_chart)
        arena = _Arena(battle, pokemon, pokemon, own)
        if "SPIKES" in named and arena.is_grounded(pokemon):
            result += (0.125, 1 / 6, 0.25)[min(3, max(1, named["SPIKES"])) - 1]
        return result

    def _status_value(self, move: Move, mon: Pokemon, foe: Pokemon, battle: Any, status: Status | None = None) -> float:
        defender = self._snapshot(foe)
        secondary = status is not None and move.category != MoveCategory.STATUS
        status = status or move.status
        if status is None or foe.status:
            return 0.0
        ignores_ability = (mon.ability in {"moldbreaker", "teravolt", "turboblaze"}
                           or move.ignore_ability) and defender.item != "abilityshield"
        # Powder immunity covers paralysis and poison as well as sleep. Type
        # and item immunities survive Mold Breaker; Overcoat does not.
        if "powder" in move.flags and (
                PokemonType.GRASS in defender.types or defender.item == "safetygoggles"
                or defender.ability == "overcoat" and not ignores_ability):
            return 0.0
        if secondary:
            # A secondary cannot apply when the damaging move has no effect,
            # or when its secondary effects are explicitly suppressed.
            if (defender.item == "covertcloak" or mon.ability == "sheerforce"
                    or defender.ability == "shielddust" and not ignores_ability
                    or self._damage(mon, foe, move, battle).high <= 0):
                return 0.0
        if defender.ability in {"purifyingsalt", "comatose"} or (defender.ability == "goodasgold" and move.category == MoveCategory.STATUS):
            return 0.0
        if "SUBSTITUTE" in _names(foe.effects) and "sound" not in move.flags and mon.ability != "infiltrator":
            return 0.0
        if defender.ability == "magicbounce" and move.category == MoveCategory.STATUS:
            return -18.0
        if mon.ability == "prankster" and move.category == MoveCategory.STATUS and PokemonType.DARK in defender.types:
            return 0.0
        arena = _Arena(battle, self._snapshot(mon), defender, True)
        fields = getattr(battle, "fields", {})
        if arena.is_grounded(defender) and Field.MISTY_TERRAIN in fields:
            return 0.0
        if status == Status.BRN:
            if PokemonType.FIRE in defender.types or defender.ability in {"waterveil", "waterbubble", "thermalexchange"}:
                return 0.0
            return 28.0 if defender.stats["atk"] > defender.stats["spa"] else 15.0
        if status in {Status.PSN, Status.TOX}:
            if (set(defender.types) & {PokemonType.STEEL, PokemonType.POISON} and mon.ability != "corrosion") or defender.ability in {"immunity", "pastelveil", "poisonheal"}:
                return 0.0
            return 23.0 if status == Status.TOX else 13.0
        if status == Status.PAR:
            if PokemonType.ELECTRIC in defender.types or defender.ability == "limber":
                return 0.0
            if move.type == PokemonType.ELECTRIC and PokemonType.GROUND in defender.types:
                return 0.0
            if move.type == PokemonType.ELECTRIC and defender.ability in {"voltabsorb", "motordrive", "lightningrod"}:
                return 0.0
            return 28.0 if self._speed(foe, battle) > self._speed(mon, battle) else 18.0
        if status == Status.SLP:
            if defender.ability in {"insomnia", "vitalspirit", "sweetveil"}:
                return 0.0
            if arena.is_grounded(defender) and Field.ELECTRIC_TERRAIN in fields:
                return 0.0
            # Sleep clauses differ by format; server legality remains authoritative.
            if any(p.status == Status.SLP for p in getattr(battle, "opponent_team", {}).values()):
                return 0.0
            return 30.0
        return 10.0

    def _utility(self, move: Move, mon: Pokemon, foe: Pokemon, battle: Any, incoming: float) -> tuple[float, list[str]]:
        value, reasons = 0.0, []
        hp = _hp(mon)
        if move.heal:
            heal = min(1 - hp, move.heal)
            value += 90 * heal - 10
            if heal:
                reasons.append(f"useful recovery of up to {heal:.0%} HP")
        if move.id == "rest":
            value += 80 * (1 - hp) + (12 if mon.status else 0) - 28
        if move.id == "wish":
            value += max(0, 0.7 - hp) * 60 - 8
        status_value = self._status_value(move, mon, foe, battle)
        for secondary in move.secondary:
            if secondary.get("status"):
                status = Status[secondary["status"].upper()]
                status_value += self._status_value(move, mon, foe, battle, status) * secondary.get("chance", 100) / 100
        value += status_value * self._accuracy(move, self._snapshot(mon), self._snapshot(foe), battle)
        if status_value > 0:
            reasons.append("status weakens the opponent")
        boosts = move.boosts if getattr(move.target, "name", "") == "SELF" else move.self_boost
        if boosts:
            gain = 0.0
            for stat, change in boosts.items():
                stage = mon.boosts.get(stat, 0)
                useful = min(6 - stage, max(0, change))
                if stat in {"atk", "spa"}:
                    cat = MoveCategory.PHYSICAL if stat == "atk" else MoveCategory.SPECIAL
                    if not any(m.category == cat for m in mon.moves.values()):
                        useful = 0
                gain += useful * {"atk": 11, "spa": 11, "spe": 9, "def": 6, "spd": 6, "accuracy": 3}.get(stat, 0)
                if change < 0:
                    value += change * (2 if stat in {"def", "spd"} else 5)
            safety = max(0.0, min(1.0, (hp - 1.5 * incoming) / 0.65))
            value += gain * safety
            if gain * safety > 8:
                reasons.append("estimated room to boost")
        side = move.side_condition
        if side and side.name in HAZARDS:
            existing = getattr(battle, "opponent_side_conditions", {})
            cap = 3 if side.name == "SPIKES" else 2 if side.name == "TOXIC_SPIKES" else 1
            layers = existing.get(side, 0)
            remaining = sum(not p.fainted for p in _opponents(battle))
            if layers < cap and self._snapshot(foe).ability != "magicbounce":
                value += max(0, remaining - 1) * (7 if side.name == "STEALTH_ROCK" else 5) * (0.8 ** layers)
                reasons.append("hazard pressure on the remaining Pokémon")
        own_hazards = len(_names(getattr(battle, "side_conditions", {})) & HAZARDS)
        if move.id in {"rapidspin", "mortalspin", "defog", "tidyup", "courtchange"} and own_hazards:
            if move.id != "rapidspin" or PokemonType.GHOST not in foe.types:
                value += 17 * own_hazards
                reasons.append("removes hazards from our side")
        if move.id in {"haze", "clearsmog", "roar", "whirlwind", "dragontail"}:
            value += max(0, sum(foe.boosts.get(s, 0) for s in ("atk", "spa", "spe"))) * 8
        if move.id in {"taunt", "encore"}:
            revealed_status = sum(m.category == MoveCategory.STATUS for m in foe.moves.values())
            value += min(22, 7 * revealed_status)
            if move.id == "encore" and foe.last_move and foe.last_move.category != MoveCategory.STATUS:
                value -= 15
        if move.is_protect_move:
            passive = 0.0625 if mon.item == "leftovers" else 0
            passive += 0.125 if foe.status in {Status.PSN, Status.TOX, Status.BRN} else 0
            value += 100 * passive - 6 - 15 * mon.protect_counter
        if move.id == "substitute":
            value += 9 if hp > 0.5 and incoming < 0.2 else -25
        if move.self_switch and getattr(battle, "available_switches", []):
            value += 10
            reasons.append("keeps the initiative by switching after the hit")
        if move.id == "knockoff":
            item = self._snapshot(foe).item or ""
            if item and not item.endswith("mask"):
                # Walls live on their items (Leftovers, Black Sludge, Eviolite, Boots).
                value += 16 if item in KNOCK_OFF_TARGETS else 8
        if move.self_destruct:
            value -= 55 * hp
        if move.entry.get("recharge"):
            value -= 22
        if "charge" in move.flags and mon.item != "powerherb":
            if not (move.id in {"solarbeam", "solarblade"} and "SUNNYDAY" in _names(getattr(battle, "weather", {}))):
                value -= 30 + 30 * incoming
        return value, reasons

    def _future_value(self, mon: Pokemon, battle: Any) -> float:
        foes = [p for p in _opponents(battle) if not p.fainted]
        if not foes:
            return 1.0
        own_moves = [m for m, _ in self._moves(mon)]
        pressure = [max((self._damage(mon, p, m, battle).expected for m in own_moves), default=0.2) for p in foes]
        return 0.85 + 0.5 * sum(min(1.0, d) for d in pressure) / len(foes)

    def _move_candidate(self, move: Move, mon: Pokemon, foe: Pokemon, battle: Any, tera: bool) -> Candidate:
        damage = self._damage(mon, foe, move, battle, tera_attacker=tera)
        threats = self._threats(foe, mon, battle, target_tera=tera)
        mean_incoming = sum(weight * inc.expected for _, weight, inc in threats)
        incoming_risk = (1 - self.risk) * mean_incoming + self.risk * max((d.expected for _, _, d in threats), default=0)
        utility, reasons = self._utility(move, mon, foe, battle, incoming_risk)
        ko = damage.ko_chance(_hp(foe))
        value, expected_taken, expected_dealt, expected_ko, faint_risk = 0.0, 0.0, 0.0, 0.0, 0.0
        future = self._future_value(mon, battle)
        for other, probability, inc in threats or [(move, 1.0, Damage(0, 0, 1))]:
            first = self._first(mon, move, foe, other, battle)
            effective = damage
            if move.id in {"suckerpunch", "thunderclap"} and other.category == MoveCategory.STATUS:
                effective = Damage(0, 0, 1)
            if move.id == "upperhand" and other.priority <= 0:
                effective = Damage(0, 0, 1)
            if other.id in {"suckerpunch", "thunderclap"} and move.category == MoveCategory.STATUS:
                inc = Damage(0, 0, 1)
            if other.id == "upperhand" and self._priority(mon, move) <= 0:
                inc = Damage(0, 0, 1)
            if other.is_protect_move and not move.breaks_protect and not (
                self._snapshot(mon).ability == "unseenfist" and "contact" in move.flags
            ):
                chance = (1 / 3) ** foe.protect_counter
                effective = Damage(damage.low, damage.high, damage.accuracy * (1 - chance))
            action_ko = effective.ko_chance(_hp(foe))
            incoming_ko = inc.ko_chance(_hp(mon))
            if move.heal and first:
                incoming_ko = inc.ko_chance(min(1.0, _hp(mon) + move.heal))
            if move.is_protect_move and not other.breaks_protect and not (
                self._snapshot(foe).ability == "unseenfist" and "contact" in other.flags
            ):
                protection = (1 / 3) ** mon.protect_counter
                incoming = inc.expected * (1 - protection)
                incoming_ko *= 1 - protection
            else:
                incoming = inc.expected * (1 - first * action_ko)
                incoming_ko *= 1 - first * action_ko
            can_act = 1 - (1 - first) * incoming_ko
            if other.id == "fakeout" and foe.first_turn and inc.high > 0 and self._snapshot(mon).ability not in {"innerfocus", "shielddust"} and mon.item != "covertcloak":
                can_act *= 1 - (1 - first) * inc.accuracy
            dealt = min(_hp(foe), effective.expected) * can_act
            score = 100 * dealt + 48 * action_ko * can_act + utility * can_act
            score -= 65 * min(_hp(mon), incoming) + 58 * incoming_ko * future
            expected_taken += probability * min(_hp(mon), incoming)
            expected_dealt += probability * dealt
            expected_ko += probability * action_ko * can_act
            faint_risk += probability * incoming_ko
            value += probability * score
        value -= self.risk * 15 * max(0, incoming_risk - mean_incoming)
        if move.recoil:
            value -= damage.expected * move.recoil * 35
        if move.drain:
            value += min(1 - _hp(mon), damage.expected * move.drain) * 45
        if move.category != MoveCategory.STATUS:
            reasons.insert(0, f"estimated damage {damage.low:.0%}-{damage.high:.0%}; accuracy {damage.accuracy:.0%}")
        if ko > 0:
            reasons.append(f"estimated knockout chance {ko:.0%}")
        reasons.append(f"expected damage taken {expected_taken:.0%}; immediate faint risk {faint_risk:.0%}")
        # A small switch branch rewards coverage without overpredicting every turn.
        bench = [p for p in _opponents(battle) if p is not foe and not p.fainted]
        if bench and damage.expected > 0:
            switched = sorted(self._damage(mon, p, move, battle, tera_attacker=tera,
                                          switched_defender=True).expected for p in bench)
            switch_probability = min(0.18, 0.04 + ko * 0.12)
            value += switch_probability * 100 * (min(1, sum(switched[:2]) / min(2, len(switched))) - min(_hp(foe), damage.expected))
        if tera:
            value -= 11  # Once-per-battle resource cost; only a material gain spends it.
            reasons.append("uses Terastal; the cost of spending it is included")
        if len(foe.moves) < 4:
            reasons.append("hidden moves/stats are estimated from the available meta")
        boosts = move.boosts if getattr(move.target, "name", "") == "SELF" else move.self_boost
        setup = sum(min(max(0, change), 6 - mon.boosts.get(stat, 0))
                    for stat, change in (boosts or {}).items()) / 4
        heal = min(1 - _hp(mon), 1 if move.id == "rest" else move.heal)
        own_hazards = len(_names(getattr(battle, "side_conditions", {})) & HAZARDS)
        side = move.side_condition
        hazard = bool(side and side.name in HAZARDS and len(_opponents(battle)) > 1 and
                      getattr(battle, "opponent_side_conditions", {}).get(side, 0) <
                      (3 if side.name == "SPIKES" else 2 if side.name == "TOXIC_SPIKES" else 1))
        status = self._status_value(move, mon, foe, battle) / 30
        for secondary in move.secondary:
            if secondary.get("status"):
                status += self._status_value(move, mon, foe, battle, Status[secondary["status"].upper()]) * secondary.get("chance", 100) / 3000
        features = {"damage": expected_dealt, "ko": expected_ko, "incoming": expected_taken,
                    "faint_risk": faint_risk, "heal": heal,
                    "setup": setup * max(0, 1 - incoming_risk / max(0.01, _hp(mon))),
                    "hazards": float(hazard),
                    "removal": min(1, own_hazards / 2) if move.id in {"rapidspin", "mortalspin", "defog", "tidyup", "courtchange"} else 0,
                    "status": max(0, status) * damage.accuracy,
                    "pivot": float(bool(move.self_switch and getattr(battle, "available_switches", []))),
                    "tera": float(tera), "accuracy": damage.accuracy,
                    "preserve": future / 1.35 * (1 - faint_risk)}
        diagnostics = {"expected_received": expected_taken, "expected_dealt": expected_dealt,
                       "ko_chance": expected_ko, "faint_risk": faint_risk,
                       "damage_low": damage.low, "damage_high": damage.high,
                       "accuracy": damage.accuracy, "has_damage_estimate": 1.0}
        return Candidate(move, tera, round(value, 3), move.id + (" + Tera" if tera else ""),
                         reasons, features, diagnostics)

    def _switch_candidate(self, mon: Pokemon, foe: Pokemon | None, battle: Any, forced: bool) -> Candidate:
        hazards = self._hazards(mon, battle)
        remaining = max(0, _hp(mon) - hazards)
        reasons = [f"estimated HP after hazards {remaining:.0%}"]
        if remaining <= 0:
            return Candidate(mon, score=-250, label=f"Switch: {mon.species}", reasons=reasons + ["would faint on entry"],
                             features={"switch": 1.0, "incoming": _hp(mon), "faint_risk": 1.0},
                             diagnostics={"expected_received": _hp(mon), "faint_risk": 1.0,
                                          "entry_hazards": hazards, "has_damage_estimate": 1.0})
        score = -9 - 70 * hazards
        fatal, incoming = 0.0, 0.0
        if foe:
            threats = self._threats(foe, mon, battle, switched=True)
            incoming = sum(weight * damage.expected for _, weight, damage in threats)
            worst = max((damage.expected for _, _, damage in threats), default=0)
            incoming = (1 - self.risk) * incoming + self.risk * worst
            pressure = max((self._damage(mon, foe, move, battle, switched_attacker=True).expected
                            for move, _ in self._moves(mon)), default=0.1)
            if forced:
                # A free replacement buys no attack. Evaluate the next turn,
                # including priority and dying before the selected move resolves.
                followups = []
                for move, _ in self._moves(mon):
                    damage = self._damage(mon, foe, move, battle, switched_attacker=True)
                    dealt = taken = dead = ko = 0.0
                    for enemy_move, weight, enemy_damage in threats:
                        first = self._first(mon, move, foe, enemy_move, battle)
                        enemy_ko = enemy_damage.ko_chance(remaining)
                        own_ko = damage.ko_chance(_hp(foe))
                        acts = 1 - (1 - first) * enemy_ko
                        dealt += weight * min(_hp(foe), damage.expected) * acts
                        ko += weight * own_ko * acts
                        dead += weight * enemy_ko * (1 - first * own_ko)
                        taken += weight * min(remaining, enemy_damage.expected) * (1 - first * own_ko)
                    followups.append((100 * dealt + 55 * ko - 80 * dead - 45 * taken, dead, move.id))
                best = max(followups, default=(0.0, 0.0, ""))
                score += best[0]
                fatal = best[1]
                reasons.append(f"expected next action: {best[2]}; risk of fainting before answering {fatal:.0%}")
            else:
                fatal = sum(weight * damage.ko_chance(remaining) for _, weight, damage in threats)
                score += 40 * min(1, pressure) - 70 * min(remaining, incoming) - 75 * fatal * self._future_value(mon, battle)
                active = getattr(battle, "active_pokemon", None)
                if active:
                    current = self._threats(foe, active, battle)
                    danger = sum(weight * damage.ko_chance(_hp(active)) for _, weight, damage in current)
                    score += danger * 20 * self._future_value(active, battle)
                    if active.ability == "regenerator":
                        score += min(1 / 3, 1 - _hp(active)) * 35
                    score -= max(0, sum(active.boosts.values())) * 2
            reasons.append(f"offensive pressure {pressure:.0%}; estimated incoming damage {incoming:.0%}")
        score += 7 * remaining
        key = str(getattr(battle, "battle_tag", id(battle)))
        previous = self._last_switch.get(key)
        if not forced and previous and getattr(battle, "turn", 0) - previous[0] <= 2:
            score -= 10
        expected_received = min(_hp(mon), hazards + (0 if forced else incoming))
        return Candidate(mon, score=round(score, 3), label=f"Switch: {mon.species}", reasons=reasons,
                         features={"switch": 1.0, "incoming": expected_received, "faint_risk": fatal,
                                   "preserve": remaining * (1 - fatal)},
                         diagnostics={"expected_received": expected_received, "faint_risk": fatal,
                                      "entry_hazards": hazards, "has_damage_estimate": 1.0,
                                      "forced_switch": float(forced)})

    def rank_actions(self, battle: Any) -> list[Candidate]:
        """Rank only actions exposed as legal by the latest server request."""
        from .state import hydrate_request
        hydrate_request(battle)
        self._reset()
        forced = bool(getattr(battle, "force_switch", False))
        mon, foe = getattr(battle, "active_pokemon", None), getattr(battle, "opponent_active_pokemon", None)
        results = []
        if not forced and mon and not mon.fainted:
            for move in getattr(battle, "available_moves", []):
                if foe:
                    results.append(self._move_candidate(move, mon, foe, battle, False))
                    if self.allow_tera and getattr(battle, "can_tera", False) and mon.tera_type:
                        results.append(self._move_candidate(move, mon, foe, battle, True))
                else:
                    results.append(Candidate(move, score=float(move.base_power * move.accuracy), label=move.id,
                                             reasons=["opponent not identified yet; simple legal action"]))
        if forced or not getattr(battle, "trapped", False):
            for switch in getattr(battle, "available_switches", []):
                if not switch.fainted:
                    results.append(self._switch_candidate(switch, foe, battle, forced))
        return sorted(results, key=lambda c: (-c.score, c.terastallize, c.label))

    def choose(self, battle: Any) -> Candidate:
        ranked = self.rank_actions(battle)
        if not ranked:
            raise ValueError("No legal action in the server's current request.")
        best = ranked[0]
        self.record_choice(battle, best)
        return best

    def record_choice(self, battle: Any, candidate: Candidate) -> None:
        """Record a selected ranked action when the runner submits it to Showdown."""
        if isinstance(candidate.action, Pokemon):
            self._last_switch[str(getattr(battle, "battle_tag", id(battle)))] = (getattr(battle, "turn", 0), candidate.action.species)

    def preview_order(self, battle: Any) -> list[int]:
        """Choose a lead against preview matchups, then retain original slot order."""
        self._reset()
        team = list(getattr(battle, "team", {}).values()) or list(getattr(battle, "teampreview_team", []))
        foes = _opponents(battle)
        if not team:
            return []
        scores = []
        for index, mon in enumerate(team, 1):
            matchups = []
            for foe in foes:
                pressure = max((self._damage(mon, foe, m, battle).expected for m, _ in self._moves(mon)), default=0.2)
                incoming = max((self._damage(foe, mon, m, battle).expected for m, _ in self._moves(foe, opponent=True)), default=0.2)
                matchups.append(min(1, pressure) - 0.7 * min(1, incoming))
            value = sum(matchups) / max(1, len(matchups))
            value += 0.12 * any(m.side_condition and m.side_condition.name in HAZARDS for m in mon.moves.values())
            value += 0.08 * any(m.self_switch for m in mon.moves.values())
            scores.append((value, -index, index))
        lead = max(scores)[2]
        return [lead] + [i for i in range(1, len(team) + 1) if i != lead]
