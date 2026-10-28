"""Bounded forward search using the official Pokémon Showdown simulator.

The battle mechanics are simulated by Showdown, rather than approximated by a
damage formula. Hidden sets remain hypotheses: monthly usage contains joint
nature/EV spreads, but its item/ability/move frequencies are only marginals.
Several coherent hypotheses are considered without claiming to know the set.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from functools import lru_cache
from typing import Any

from poke_env.battle import Move, Pokemon
from poke_env.data import GenData, to_id_str

from .engine import Candidate, STATS, _opponents
from .strategy import base_species_id

LOG = logging.getLogger(__name__)


CPU_LEVELS = {"low": .25, "medium": .5, "high": 1.}


def worker_capacity(level: str = "high") -> int:
    """Simulator threads for a CPU usage level, always leaving room for the OS."""
    cores = os.cpu_count() or 2
    usable = max(1, min(10, cores - 2))
    return max(1, min(usable, round(cores * CPU_LEVELS.get(level, .5))))


def worker_share(battles: int, level: str = "high") -> int:
    """Split the simulator threads between simultaneous battles."""
    return max(1, worker_capacity(level) // max(1, int(battles)))


_LEAD_PRIOR: dict | None = None


def _lead_prior() -> dict:
    """assets/lead_priors.json (tools/top_research.py leads); empty if absent."""
    global _LEAD_PRIOR
    if _LEAD_PRIOR is None:
        try:
            raw = json.loads((Path(__file__).resolve().parents[1] / "assets" / "lead_priors.json").read_text(encoding="utf-8"))
            _LEAD_PRIOR = {**raw, "species": {to_id_str(name): rate for name, rate in raw.get("species", {}).items()}}
        except (OSError, ValueError):
            _LEAD_PRIOR = {}
    return _LEAD_PRIOR


def material_risk(battle: Any, base: float) -> float:
    """Risk aversion follows the material score. Ahead by two Pokémon, protect
    the lead (51 ladder losses came after being 2 up); behind by two, the safe
    line loses anyway, so variance is worth more."""
    own = sum(not mon.fainted for mon in getattr(battle, "team", {}).values())
    foes = len(getattr(battle, "teampreview_opponent_team", None) or ()) or 6
    foes -= sum(mon.fainted for mon in getattr(battle, "opponent_team", {}).values())
    lead = own - foes
    shift = .2 if lead >= 2 else .1 if lead == 1 else -.1 if lead == -1 else -.2 if lead <= -2 else 0.
    return max(0.0, min(1.0, base + shift))


class SearchUnavailable(RuntimeError):
    """The exact forward search could not execute; callers must report this."""


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _distribution(data: dict, name: str) -> list[tuple[str, float]]:
    return sorted(((key, _number(value)) for key, value in data.get(name, {}).items()
                   if _number(value) > 0), key=lambda pair: pair[1], reverse=True)


def _enum_id(value: Any) -> str:
    return to_id_str(getattr(value, "name", str(value)))


@lru_cache(maxsize=16)
def _published_catalog(format_id: str) -> dict:
    """Local public authored sets, with no network access on a battle turn."""
    if not format_id.startswith('gen9') or not format_id.isalnum():
        return {}
    path = Path(__file__).resolve().parents[1] / 'assets/opponent_sets' / (format_id + '.json')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        return raw if raw.get('schema') == 1 and raw.get('format_id') == format_id else {}
    except (OSError, ValueError, TypeError):
        return {}


class SimulatorSearch:
    """A persistent JSONL worker; safe to use from one player's decision thread.

    Search considers both moves and switches for both players, handles pivot
    replacement requests, and uses a bounded two-turn adversarial beam. A small
    deterministic ensemble samples damage rolls/misses and hidden sets. This is
    a search policy with an evaluation function, not an optimal-play guarantee.
    """

    def __init__(self, meta: Any = None, node_path: str | Path | None = None,
                 simulator_path: str | Path | None = None,
                 time_budget_ms: int = 10000, depth: int = 2, workers: int | None = None):
        self.meta = meta
        self.node_path = str(node_path) if node_path else None
        self.simulator_path = str(simulator_path) if simulator_path else None
        self.time_budget_ms = max(200, min(15000, int(time_budget_ms)))
        self.depth = max(1, min(2, int(depth)))
        # Threads live inside one Node process, so a single kill on STOP also
        # ends every simulation immediately. Leave capacity for the UI/OS.
        capacity = worker_capacity()
        self.workers = max(1, min(capacity, int(workers))) if workers is not None else (capacity if self.time_budget_ms >= 3000 else 1)
        self.last_diagnostics: dict[str, Any] = {}
        self.preferences: dict[str, dict[str, float]] = {}
        self.feedback: dict[str, Any] = {}
        self.strategy_profile: dict[str, Any] = {}
        self.opponent_profile: dict[str, Any] = {}
        self.opponent_scout: dict[str, Any] = {}
        # base species -> move it repeated while staying in (Choice item tell).
        self.choice_locked: dict[str, str] = {}
        self.foe_habit: dict = {}
        # "v2" (3.13): simultaneous choice + Foul Play-style evaluation; "legacy" for A/B.
        self.brain = "v2"
        # Hypotheses (of 4) that model an inferred Choice lock; the rest stay free.
        self.choice_lock_share = 2
        # Extra time the first complete round may take; 0 under timer pressure.
        self.grace_ms = 2500
        # Prior weight of an opposing voluntary switch (see runner calibration).
        self.foe_switch_prior = .45
        self.reply_temperature = 28.
        self.safety_base = .08
        self._joint_cache: dict[tuple, list] = {}
        self._process: subprocess.Popen | None = None
        self._out: queue.Queue = queue.Queue()
        self._stderr: list[str] = []
        self._lock = threading.Lock()
        self._aborted = threading.Event()

    def _check_cancelled(self) -> None:
        if self._aborted.is_set():
            raise SearchUnavailable("Search cancelled because the session is closing.")

    def _start(self) -> None:
        self._check_cancelled()
        if self._process is not None and self._process.poll() is None:
            return
        bundled = Path(__file__).resolve().parents[1] / "runtime/node.exe"
        node = self.node_path or os.environ.get("SHOWDOWN_NODE_PATH") or (str(bundled) if bundled.is_file() else None) or shutil.which("node")
        if not node:
            windows_node = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "nodejs/node.exe"
            if windows_node.is_file():
                node = str(windows_node)
        if not node:
            raise SearchUnavailable("Search unavailable: run start.bat to set up the simulator and Node.js.")
        environment = dict(os.environ)
        environment["SHOWDOWN_SEARCH_WORKERS"] = str(self.workers)
        if self.simulator_path:
            environment["SHOWDOWN_SIMULATOR_PATH"] = self.simulator_path
        self._out = queue.Queue()
        self._stderr = []
        # Below-normal priority: the PC stays responsive while the bot thinks.
        flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 0
        try:
            self._process = subprocess.Popen(
                [node, str(Path(__file__).with_name("simulator_worker.js"))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", bufsize=1, env=environment,
                creationflags=flags,
            )
        except OSError as exc:
            raise SearchUnavailable(f"Could not start the simulator: {exc}") from exc
        process, out = self._process, self._out
        if self._aborted.is_set():
            process.kill()
            self.close()
            self._check_cancelled()

        def reader():
            try:
                for line in process.stdout:
                    out.put(line)
            except (OSError, ValueError):
                pass
            finally:
                out.put(None)

        def errors():
            try:
                for line in process.stderr:
                    self._stderr.append(line.strip())
                    del self._stderr[:-12]
            except (OSError, ValueError):
                pass

        threading.Thread(target=reader, name="showdown-search-output", daemon=True).start()
        threading.Thread(target=errors, name="showdown-search-errors", daemon=True).start()
        try:
            ready = self._read(20)
        except SearchUnavailable:
            self.close()
            raise
        if not ready.get("ready"):
            self.close()
            raise SearchUnavailable(ready.get("error", "The simulator did not get ready."))

    def _read(self, timeout: float) -> dict:
        self._check_cancelled()
        try:
            line = self._out.get(timeout=timeout)
        except queue.Empty as exc:
            raise SearchUnavailable("The simulator timed out.") from exc
        self._check_cancelled()
        if line is None:
            raise SearchUnavailable("The simulator exited: " + " ".join(self._stderr)[-1000:])
        try:
            return json.loads(line)
        except (ValueError, TypeError) as exc:
            raise SearchUnavailable("Invalid response from the simulator.") from exc

    def _rpc(self, payload: dict) -> dict:
        self._check_cancelled()
        with self._lock:
            self._check_cancelled()
            self._start()
            process = self._process
            self._check_cancelled()
            if process is None:
                raise SearchUnavailable("The simulator was closed.")
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
                process.stdin.flush()
                response = self._read((self.time_budget_ms + self.grace_ms) / 1000 + 8)
            except (OSError, ValueError, SearchUnavailable) as exc:
                self.close()
                raise SearchUnavailable(str(exc)) from exc
            if response.get("error"):
                raise SearchUnavailable(response["error"])
            return response

    def abort(self) -> None:
        """Cancel an in-flight RPC immediately; this session cannot restart it."""
        self._aborted.set()
        self._out.put(None)
        process = self._process
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        # Reap handles off the caller/UI thread, including an idle worker.
        threading.Thread(target=self.close, name="showdown-search-cleanup", daemon=True).start()

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe:
                pipe.close()

    def healthcheck(self) -> dict:
        """Start the worker before login/matchmaking and report its version."""
        return self._rpc({"op": "health"})

    def ready(self) -> dict:
        return self.healthcheck()

    def validate_team(self, text: str, format_id: str) -> list[str]:
        """Use official tier rules rather than a second, stale ban list."""
        return self._rpc({"op": "validate", "team": text, "format": format_id}).get("errors", [])

    def _usage(self, mon: Pokemon, engine: Any) -> dict:
        meta = self.meta or getattr(engine, "meta", None)
        return meta.species(mon.species) or {} if meta else {}

    def _joint_sets(self, mon: Pokemon, engine: Any, usage: dict) -> list:
        """Keep EVs, nature, item and moves from the same published build.

        Observed moves/items/abilities eliminate incompatible templates. These
        remain hypotheses: a custom opposing build can reject the entire list.
        Smogon usage weights candidate builds; it is never called a joint set.
        """
        meta = self.meta or getattr(engine, 'meta', None)
        catalog = _published_catalog(getattr(meta, 'format_id', '') or '')
        if not catalog:
            return []
        evidence = self.preferences.get(base_species_id(mon.species), {})
        learned = evidence.get('moves', {}) if isinstance(evidence, dict) else {}
        confidence = min(.25, max(0, _number(evidence.get('confidence', 0)))) if isinstance(evidence, dict) else 0
        cache_key = (getattr(meta, 'format_id', ''), getattr(meta, 'month', ''), getattr(meta, 'rating', 0),
                     mon.species, tuple(sorted(mon.moves)), mon.item, mon.ability, confidence, tuple(sorted(learned.items())))
        if cache_key in self._joint_cache:
            return self._joint_cache[cache_key]
        key = base_species_id(mon.species)
        from .opponent_sets import published_set
        records = []
        for species, sets in catalog.get('sets', {}).items():
            if base_species_id(species) == key and isinstance(sets, dict):
                records.extend((species, value) for value in sets.values() if isinstance(value, dict))
        for team in catalog.get('teams', []):
            records.extend((value['species'], value) for value in team.get('data', [])
                           if isinstance(value, dict) and value.get('species') and base_species_id(value['species']) == key)
        known = set(mon.moves)
        output, seen = [], set()
        for species, record in records:
            slots = [{to_id_str(value) for value in (slot if isinstance(slot, list) else [slot])}
                     for slot in record.get('moves', [])]
            if len(slots) != 4 or not known <= set().union(*slots):
                continue
            for variant in range(3):
                try:
                    member = published_set(species, record, variant)
                    moves = [to_id_str(move) for move in member.moves]
                    for move in known - set(moves):
                        slot = next((i for i, values in enumerate(slots) if move in values and moves[i] not in known), None)
                        if slot is not None: moves[slot] = move
                    if not known <= set(moves) or len(set(moves)) != 4:
                        continue
                    if mon.ability and member.ability and to_id_str(member.ability) != mon.ability:
                        continue
                    unknown_item = GenData.from_gen(mon.gen).UNKNOWN_ITEM
                    if mon.item and mon.item != unknown_item and to_id_str(member.item) != mon.item:
                        continue
                    member.moves = moves
                    signature = (tuple(moves), member.item, member.nature, tuple(member.evs.values()))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    items, observed_moves = _distribution(usage, 'Items'), _distribution(usage, 'Moves')
                    item_peak, move_peak = (items[0][1] if items else 1), (observed_moves[0][1] if observed_moves else 1)
                    item_prior = next((value for name, value in items if to_id_str(name) == to_id_str(member.item)), 0) / item_peak
                    move_prior = sum(next((value for name, value in observed_moves if to_id_str(name) == move), 0) / move_peak for move in moves) / 4
                    learned_prior = sum(min(1, max(0, _number(learned.get(move)))) for move in moves) / 4
                    output.append((item_prior * 2 + (1 - confidence) * move_prior + confidence * learned_prior, member))
                except (ValueError, TypeError, KeyError):
                    continue
        output.sort(key=lambda row: -row[0])
        # Distinct roles/items precede slash variants of one identical build.
        primary, repeated, items = [], [], set()
        for _, member in output:
            signature = (to_id_str(member.item), tuple(member.moves))
            (repeated if signature in items else primary).append(member)
            items.add(signature)
        if len(self._joint_cache) > 512:
            self._joint_cache.clear()
        self._joint_cache[cache_key] = primary + repeated
        return primary + repeated

    def _set(self, mon: Pokemon, engine: Any, index: int = 0, opponent: bool = False) -> dict:
        usage = self._usage(mon, engine) if opponent else {}
        joint = self._joint_sets(mon, engine, usage) if opponent else []
        template = joint[index % len(joint)] if joint else None
        revealed = list(mon.moves.values())
        physical = _number(mon.base_stats.get("atk")) >= _number(mon.base_stats.get("spa"))
        evs, nature = mon.evs, mon.nature
        spreads = _distribution(usage, "Spreads")
        if evs is None and spreads:
            for offset in range(len(spreads)):
                try:
                    label, _ = spreads[(index + offset) % min(4, len(spreads))]
                    spread_nature, numbers = label.split(":", 1)
                    spread = [int(x) for x in numbers.split("/")]
                    if len(spread) == 6 and all(0 <= x <= 252 for x in spread) and sum(spread) <= 510:
                        evs, nature = spread, spread_nature
                        physical = spread[1] >= spread[3]
                        break
                except (ValueError, TypeError):
                    continue
        if evs is None:
            # Missing tier data must not give every hidden Pokémon the same
            # fast, zero-HP set. Slow bulky species and special walls have
            # materially different plausible spreads. Feedback can distinguish
            # these hypotheses using official damage ranges, without changing
            # game formulas or claiming that the hidden set has been revealed.
            slow = _number(mon.base_stats.get("spe")) <= 75
            defensive = max(_number(mon.base_stats.get("def")), _number(mon.base_stats.get("spd"))) >= max(_number(mon.base_stats.get("atk")), _number(mon.base_stats.get("spa"))) + 18
            variant = index % 6
            attack_stat = "atk" if physical else "spa"
            offense = {key: 0 for key in STATS}
            if variant == 0:
                offense.update(hp=252 if slow or defensive else 0)
                if defensive:
                    offense["def" if _number(mon.base_stats.get("def")) >= _number(mon.base_stats.get("spd")) else "spd"] = 252
                    nature = nature or ("Impish" if physical else "Calm")
                else:
                    offense[attack_stat] = 252
                    offense["def" if physical else "spd"] = 4
                    offense["spe"] = 0 if slow else 252
                    nature = nature or ("Adamant" if physical and slow else "Jolly" if physical else "Modest" if slow else "Timid")
            elif variant == 1:
                offense[attack_stat], offense["spe"], offense["def"] = 252, 252, 4
                nature = nature or ("Jolly" if physical else "Timid")
            elif variant == 2:
                offense["hp"], offense[attack_stat], offense["spd"] = 252, 252, 4
                nature = nature or ("Adamant" if physical else "Modest")
            elif variant == 3:
                offense["hp"], offense["spd"], offense["def"] = 252, 252, 4
                nature = nature or ("Careful" if physical else "Calm")
            elif variant == 4:
                offense["hp"], offense["def"], offense["spd"] = 252, 252, 4
                nature = nature or ("Impish" if physical else "Bold")
            else:
                offense["hp"], offense[attack_stat], offense["spd"] = 252, 252, 4
                nature = nature or ("Careful" if physical else "Calm")
            evs = [offense[key] for key in STATS]
        moves = [move.id for move in revealed]
        priors = {move.id: 1.0 for move in revealed}
        if opponent and len(moves) < 4:
            entries = _distribution(usage, "Moves")
            evidence = self.preferences.get(mon.species, {})
            learned_moves = evidence.get("moves", evidence) if isinstance(evidence, dict) else {}
            confidence = min(.25, max(0.0, _number(evidence.get("confidence", .25)))) if evidence else 0.0
            if confidence:
                peak = entries[0][1] if entries else 1.0
                entries = sorted(((name, frequency * (1 - confidence) + peak * confidence * min(1.0, max(0.0, _number(learned_moves.get(to_id_str(name), 0.0)))))
                                  for name, frequency in entries), key=lambda pair: pair[1], reverse=True)
                existing = {to_id_str(name) for name, _ in entries}
                for name, frequency in learned_moves.items():
                    move_id = to_id_str(name)
                    if move_id not in existing and move_id in mon.learnset and _number(frequency) > 0:
                        entries.append((move_id, peak * confidence * min(1.0, _number(frequency))))
                entries.sort(key=lambda pair: pair[1], reverse=True)
            # A four-move hypothesis, never an impossible eight-move opponent.
            # Keep coverage alternatives in different hypotheses, and retain
            # every revealed move regardless of its population frequency.
            legal = []
            gen_data = GenData.from_gen(mon.gen)
            for name, frequency in entries:
                move_id = to_id_str(name)
                if move_id in moves or move_id not in gen_data.moves:
                    continue
                move = Move(move_id, gen=mon.gen)
                category = getattr(move.category, "name", "")
                matching = category == "STATUS" or (category == "PHYSICAL") == physical
                legal.append((move_id, frequency, matching))
            legal.sort(key=lambda row: (row[2], row[1]), reverse=True)
            # Move marginals are uncorrelated with spreads. Rotate the last
            # coverage choice only; do not invent a claim of joint frequencies.
            if index and len(legal) >= 4:
                rotation = min(index, len(legal) - 2)
                legal = legal[:1] + legal[1 + rotation:] + legal[1:1 + rotation]
            for move_id, frequency, _ in legal:
                if len(moves) >= 4:
                    break
                moves.append(move_id)
                priors[move_id] = min(1.0, frequency / max(1.0, entries[0][1]))
            if len(moves) < 4:
                for move, frequency in engine._moves(mon, opponent=True):
                    if move.id not in moves:
                        moves.append(move.id)
                        priors[move.id] = frequency
                    if len(moves) >= 4:
                        break
        if not moves:
            moves = ["struggle"]
        ability = mon.ability
        if ability is None:
            abilities = _distribution(usage, "Abilities")
            ability = abilities[min(index, len(abilities) - 1)][0] if abilities else (mon.possible_abilities[0] if mon.possible_abilities else "")
            if _enum_id(mon.status) == "brn" and "guts" in mon.possible_abilities and index == 0:
                ability = "guts"
        item = mon.item
        if item == GenData.from_gen(mon.gen).UNKNOWN_ITEM:
            items = _distribution(usage, "Items")
            # Vary only plausible alternatives, not a one-percent niche item.
            items = [row for row in items if row[1] >= items[0][1] * 0.25] if items else []
            item = items[min(index, len(items) - 1)][0] if items else ""
            if _enum_id(mon.status) == "brn" and ability == "guts" and index == 0:
                item = "flameorb"
            elif not items:
                variant = index % 6
                has_status = any(Move(move, gen=mon.gen).category.name == "STATUS" for move in moves if move != "struggle")
                if variant in (2, 5):
                    item = "choiceband" if physical else "choicespecs"
                elif variant == 3 and not has_status:
                    item = "assaultvest"
                elif variant in (3, 4):
                    item = "leftovers"
        tera_type = getattr(mon.tera_type, "name", None)
        if not tera_type and opponent:
            teras = _distribution(usage, "Tera Types")
            tera_type = teras[min(index, len(teras) - 1)][0] if teras else None
        if template:
            if mon.evs is None: evs = [template.evs[key] for key in STATS]
            if mon.nature is None: nature = template.nature
            moves = template.moves
            priors = {move: 1.0 if move in mon.moves else .7 for move in moves}
            if mon.ability is None:
                ability = to_id_str(template.ability) or (mon.possible_abilities[0] if mon.possible_abilities else '')
            if mon.item == GenData.from_gen(mon.gen).UNKNOWN_ITEM: item = template.item
            if not mon.tera_type and template.tera_type: tera_type = template.tera_type
        if opponent and index < 4 and self.opponent_scout:
            # This player's own past sets for the species beat population usage.
            from .scouting import scouted_set
            seen = scouted_set(self.opponent_scout, mon.species)
            if seen:
                known = [move.id for move in revealed]
                extra = [m for m in seen["moves"] if m not in known and m in GenData.from_gen(mon.gen).moves]
                rest = [m for m in moves if m not in known and m not in extra]
                moves = (known + extra + rest)[:4]
                priors = {**{m: priors.get(m, .6) for m in moves}, **{m: .9 for m in extra}, **{m: 1. for m in known}}
                if mon.item == GenData.from_gen(mon.gen).UNKNOWN_ITEM and seen["item"]:
                    item = seen["item"]
                if mon.ability is None and seen["ability"]:
                    ability = seen["ability"]
                if not getattr(mon, "tera_type", None) and seen["tera"]:
                    tera_type = seen["tera"]
        locked = self.choice_locked.get(base_species_id(mon.species)) if opponent else None
        if locked and mon.item == GenData.from_gen(mon.gen).UNKNOWN_ITEM and index < self.choice_lock_share:
            # Repeating one attack turn after turn with an unrevealed item is the
            # Choice-lock tell. Model the lock so switching into it is priced right.
            # It stays a hypothesis: the others keep the free coverage (replay vs
            # beans727: three Dragon Pulses, then Flamethrower on the "safe" Kingambit).
            category = getattr(Move(locked, gen=mon.gen).category, "name", "")
            item = "choicescarf" if index % 2 else ("choiceband" if category == "PHYSICAL" else "choicespecs")
            if locked not in moves:
                moves = ([locked] + [m for m in moves if m != locked])[:4]
            priors[locked] = 1.0
        tera_type = (tera_type or getattr(mon.type_1, "name", "Normal")).title()
        stats = {key: value for key, value in mon.stats.items() if value is not None}
        status = _enum_id(mon.status) if mon.status else ""
        fainted = mon.fainted
        fraction = min(1.0, max(0.0, mon.current_hp_fraction)) if mon.max_hp else (0.0 if fainted else 1.0)
        last = getattr(mon, "last_move", None)
        return {
            "set": {"species": mon.species, "name": "", "moves": moves[:4],
                    "nature": (nature or "Serious").title(), "evs": dict(zip(STATS, evs)),
                    "ivs": dict(zip(STATS, mon.ivs or ([template.ivs[key] for key in STATS] if template else [31] * 6))), "level": mon.level,
                    "ability": to_id_str(ability or ""), "item": to_id_str(item or ""),
                    "teraType": tera_type},
            "hp": fraction, "actualHp": mon.current_hp if not opponent else None,
            "actualMaxHp": mon.max_hp if not opponent and mon.max_hp else None,
            "stats": stats, "fainted": fainted, "status": status,
            "statusCounter": mon.status_counter, "boosts": dict(mon.boosts),
            "effects": {_enum_id(k): v for k, v in mon.effects.items()},
            "lastMove": (locked if opponent and locked else last.id if last else ""), "activeTurns": getattr(mon, "_active_turns", 1),
            "activeMoveActions": max(0, getattr(mon, "_active_turns", 1) - 1),
            "mustRecharge": bool(getattr(mon, "must_recharge", False)),
            "protectCounter": getattr(mon, "protect_counter", 0),
            "preparingMove": getattr(getattr(mon, "preparing_move", None), "id", ""),
            "terastallized": tera_type if getattr(mon, "_terastallized", False) else "",
            "types": [getattr(kind, "name", str(kind)).title() for kind in mon.types],
            "pp": {move.id: move.current_pp for move in revealed}, "priors": priors,
            "revealedMoves": [move.id for move in revealed],
            "setSource": "published-build-hypothesis" if template else "usage-marginals-hypothesis" if opponent and usage else "observed-or-estimated",
        }

    def _payload(self, battle: Any, candidates: list[Candidate], engine: Any) -> dict:
        if getattr(battle, "gen", 9) != 9:
            raise SearchUnavailable("Simulator search is implemented for Generation 9 singles.")
        own_active = battle.active_pokemon
        foe_active = battle.opponent_active_pokemon
        if own_active is None or foe_active is None:
            raise SearchUnavailable("Search needs the active Pokémon; lead selection uses the preview policy.")
        own = [own_active] + [mon for mon in battle.team.values() if mon is not own_active]
        foes = [foe_active] + [mon for mon in _opponents(battle) if mon is not foe_active]
        unique_foes = []
        seen = set()
        for mon in foes:
            # Pokemon may change form after preview. _opponents already merges
            # baseSpecies; this extra check handles duplicate object references.
            if id(mon) not in seen:
                seen.add(id(mon))
                unique_foes.append(mon)
        if len(own) > 6 or len(unique_foes) > 6:
            raise SearchUnavailable("Search supports singles teams of up to six Pokémon.")
        own_data = [self._set(mon, engine) for mon in own]
        root_actions = []
        for index, candidate in enumerate(candidates):
            if isinstance(candidate.action, Move):
                root_actions.append({"key": index, "move": candidate.action.id,
                                     "tera": candidate.terastallize})
            elif isinstance(candidate.action, Pokemon):
                try:
                    position = next(i for i, mon in enumerate(own) if mon is candidate.action)
                except StopIteration as exc:
                    raise SearchUnavailable("Switch to a Pokémon that is not on the team.") from exc
                root_actions.append({"key": index, "switch": position})
        request = getattr(battle, "last_request", {}) or {}
        own_request = (request.get("active") or [{}])[0] or {}
        available = [{"id": move.id, "pp": move.current_pp}
                     for move in getattr(battle, "available_moves", [])]
        if own_request.get("moves"):
            available = own_request["moves"]
        forced = bool(getattr(battle, "force_switch", False))
        if isinstance(getattr(battle, "force_switch", False), list):
            forced = any(battle.force_switch)
        conditions = lambda values: {_enum_id(key): value for key, value in (values or {}).items()}
        payload = {
            "op": "search", "gen": 9, "turn": int(getattr(battle, "turn", 1)),
            "own": own_data,
            "opponents": [[self._set(mon, engine, i, True) for mon in unique_foes]
                          for i in range(6 if self.workers > 1 or self.feedback.get("observed_damage") else 3)],
            "actions": root_actions, "available": available, "forceSwitch": forced,
            "trapped": bool(getattr(battle, "trapped", False)),
            "canTera": (bool(getattr(battle, "can_tera", False)) or (forced and not any(getattr(mon, "_terastallized", False) for mon in own))) and bool(getattr(engine, "allow_tera", True)),
            "opponentCanTera": not getattr(battle, "opponent_used_tera", False) and not any(getattr(mon, "_terastallized", False) for mon in unique_foes),
            "sideConditions": [conditions(getattr(battle, "side_conditions", {})),
                               conditions(getattr(battle, "opponent_side_conditions", {}))],
            "weather": conditions(getattr(battle, "weather", {})),
            "fields": conditions(getattr(battle, "fields", {})),
            "depth": self.depth, "timeBudgetMs": self.time_budget_ms,
            "breadth": 3 if self.time_budget_ms >= 5000 else 2,
            "graceMs": self.grace_ms,
            "foeSwitchPrior": self.foe_switch_prior,
            "foeHabit": self.foe_habit or None,
            "brain": self.brain,
            "replyTemperature": self.reply_temperature, "safetyBase": self.safety_base,
            "hypothesisLimit": 6 if self.workers > 1 else 3,
            "allResponses": self.workers > 1,
            "risk": material_risk(battle, getattr(engine, "risk", 0.35)),
            "feedback": self.feedback,
            "strategy": self.strategy_profile,
            "opponentStrategy": self.opponent_profile,
        }
        return payload

    def rank(self, battle: Any, candidates: list[Candidate], engine: Any) -> list[Candidate]:
        if not candidates:
            return []
        payload = self._payload(battle, candidates, engine)
        try:
            response = self._rpc(payload)
        except Exception as exc:
            if "Choices are done" in str(exc):
                # Not reproducible from synthetic states: keep the real one.
                try:
                    debug = Path(__file__).resolve().parents[1] / "cache" / "search_errors"
                    debug.mkdir(parents=True, exist_ok=True)
                    (debug / f"choices-done-{int(time.time() * 1000)}.json").write_text(
                        json.dumps(payload, default=str), encoding="utf-8")
                except OSError:
                    pass
            raise
        self.last_diagnostics = response.get("diagnostics", {})
        dump = os.environ.get("SHOWDOWN_DUMP_DIR")
        if dump:
            # Audit only (tools/bench.py): replayable search inputs and outputs.
            try:
                folder = Path(dump)
                folder.mkdir(parents=True, exist_ok=True)
                name = f"{getattr(battle, 'battle_tag', 'battle')}-t{getattr(battle, 'turn', 0)}-{int(time.time() * 1000)}.json"
                (folder / name).write_text(json.dumps({"payload": payload, "response": response,
                    "labels": [c.label for c in candidates]}, default=str), encoding="utf-8")
            except OSError:
                pass
        results = {row["key"]: row for row in response.get("results", [])}
        if len(results) != len(candidates):
            raise SearchUnavailable("The simulator did not evaluate every legal option.")
        for key, candidate in enumerate(candidates):
            row = results[key]
            if not row.get("legal", True):
                candidate.score = -1e6
                candidate.reasons.append("The rebuilt simulator state did not accept this option.")
                candidate.diagnostics["search_legal"] = 0.0
                continue
            candidate.score = float(row["score"])
            candidate.diagnostics.update({
                "base_score": candidate.score, "search_score": candidate.score,
                "search_nodes": float(response.get("diagnostics", {}).get("nodes", 0)),
                "search_depth": float(row.get("depth", 1)),
                "search_samples": float(row.get("samples", 1)),
                "search_worst": float(row.get("worst", candidate.score)),
                "search_mean": float(row.get("mean", candidate.score)),
                "search_std": float(row.get("deviation", 0)),
                "search_win_probability": float(row.get("winProbability", 0)),
                "search_loss_probability": float(row.get("lossProbability", 0)),
                "search_damage_std": float(row.get("damageStd", 0)),
                "search_received_std": float(row.get("receivedStd", 0)),
                "search_received_worst": float(row.get("receivedWorst", row.get("received", 0))),
                "search_faint_risk_worst": float(row.get("faintRiskWorst", row.get("faintRisk", 0))),
                "search_opponent_responses": float(row.get("responses", 0)),
                "search_workers": float(response.get("diagnostics", {}).get("workers", 1)),
                "search_scenarios": float(response.get("diagnostics", {}).get("scenarios", 0)),
                "search_legal": 1.0,
                "forced_loss_trade": bool(row.get("terminalTrade", False)),
            })
            candidate.reasons.insert(0, f"Simulation: {row.get('depth', 1)} turns, value {candidate.score:.1f}; worst case {row.get('worst', candidate.score):.1f}.")
            if row.get("hazardRetention") is not None:
                candidate.diagnostics["search_hazard_retention"] = float(row["hazardRetention"])
            # Recorded outcomes refer to the original active/target even when
            # a switch/pivot changes the active slot within the simulated turn.
            candidate.diagnostics.update({"expected_received": float(row.get("received", 0)),
                                          "expected_dealt": float(row.get("dealt", 0)),
                                          "faint_risk": float(row.get("faintRisk", 0)),
                                          "ko_chance": float(row.get("koChance", 0))})
        if not any(c.diagnostics.get("search_legal") for c in candidates):
            raise SearchUnavailable("No legal option could be reproduced in the simulator.")
        return sorted(candidates, key=lambda candidate: candidate.score, reverse=True)

    def _lead_weights(self, opponents: list) -> list[float]:
        """Who the opponent probably leads with: this player's scouted leads,
        else a population prior (species lead rate x preview slot 1)."""
        scouted = [float((self.opponent_scout or {}).get("leads", {}).get(base_species_id(mon.species), 0)) for mon in opponents]
        if any(scouted):
            return scouted
        prior = _lead_prior()
        if not prior:
            return scouted
        rates = prior.get("species", {})
        default = float(prior.get("default_rate", 1 / 6))
        slot1 = max(1.0, float(prior.get("slot1_rate", 1 / 6)) * 6)
        weights = [rates.get(to_id_str(mon.species), rates.get(base_species_id(mon.species), default)) *
                   (slot1 if index == 0 else 1.0) for index, mon in enumerate(opponents)]
        total = sum(weights) or 1.0
        return [round(value / total, 4) for value in weights]

    def preview_order(self, battle: Any, engine: Any) -> list[int]:
        """Simulate a first-turn lead matrix, preserving entry ability events."""
        team = list(getattr(battle, "team", {}).values()) or list(getattr(battle, "teampreview_team", []))
        opponents = _opponents(battle)
        if not team or not opponents:
            raise SearchUnavailable("The server did not send the teams needed to evaluate the preview.")
        payload = {
            "op": "preview", "preview": True, "gen": 9, "turn": 1,
            "own": [self._set(mon, engine) for mon in team],
            "opponents": [[self._set(mon, engine, 0, True) for mon in opponents]],
            "canTera": bool(getattr(engine, "allow_tera", True)), "opponentCanTera": True,
            "risk": getattr(engine, "risk", .35), "timeBudgetMs": min(3000, max(1000, self.time_budget_ms * 2)),
            "strategy": self.strategy_profile,
            "foeLeadWeights": self._lead_weights(opponents),
            "brain": self.brain,
        }
        response = self._rpc(payload)
        self.last_diagnostics = response.get("diagnostics", {})
        order = response.get("order", [])
        if sorted(order) != list(range(1, len(team) + 1)):
            raise SearchUnavailable("The simulator returned an incomplete preview order.")
        return order

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
