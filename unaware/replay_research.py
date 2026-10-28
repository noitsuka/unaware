"""Small, recent public replay samples; revealed facts are not hidden builds.

The official search endpoint orders by upload date when sort is omitted.
Rating sorting alone often returns old battles, so every source is filtered
by both its actual rating and upload timestamp before a log is requested.
No player names, nicknames, chat, full logs, EVs, IVs or guessed stats persist.
Call refresh from a background worker; load never performs network requests.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
from itertools import combinations
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .teams import SUPPORTED_FORMATS, to_id

BASE_URL = "https://replay.pokemonshowdown.com/"
WINDOW_DAYS = 120
MAX_BYTES = 4 * 1024 * 1024
MAX_REPLAYS = 24
HAZARDS = {"stealthrock", "spikes", "toxicspikes", "stickyweb"}
PHAZING = {"whirlwind", "roar", "dragontail", "circlethrow"}
RECOVERY = {"recover", "roost", "slackoff", "softboiled", "milkdrink", "shoreup", "synthesis", "moonlight", "morningsun", "rest", "strengthsap", "wish", "healorder"}
ROLE_MOVES = {"hazards": HAZARDS, "removal": {"rapidspin", "defog", "mortalspin", "tidyup", "courtchange"},
              "phazing": PHAZING, "recovery": RECOVERY,
              "screens": {"reflect", "lightscreen", "auroraveil"},
              "pivot": {"uturn", "voltswitch", "flipturn", "partingshot", "teleport", "chillyreception", "batonpass"}}


class ReplayResearchError(RuntimeError):
    """An official replay source was unavailable or unsuitable."""


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


@lru_cache(maxsize=1024)
def _roles(move: str) -> tuple[str, ...]:
    roles = {name for name, moves in ROLE_MOVES.items() if move in moves}
    from poke_env.data import GenData
    data = GenData.from_gen(9).moves.get(move, {})
    if data.get("category") in {"Physical", "Special"}:
        roles.add("attacker")
        if data.get("priority", 0) > 0:
            roles.add("priority")
    boosts = data.get("boosts") or (data.get("self") or {}).get("boosts") or {}
    if data.get("category") == "Status" and data.get("target") == "self" and any(value > 0 for value in boosts.values()):
        roles.add("setup")
    if data.get("category") == "Status" and data.get("status"):
        roles.add("status")
    return tuple(sorted(roles))


def _slot(actor: str) -> str:
    match = re.match(r"^(p[12][a-z]?):", actor)
    return match[1] if match else ""


def _species(details: str) -> str:
    return to_id(details.split(",", 1)[0])[:50]


@lru_cache(maxsize=1024)
def _base_species(name: str) -> str:
    from poke_env.data import GenData
    return to_id(GenData.from_gen(9).pokedex.get(name, {}).get("baseSpecies", name))


def _archetype(team: dict, turns: int) -> dict:
    """Conservative inference from positive observations, never missing moves."""
    counts = Counter(role for member in team["revealed"] for role in member["roles"])
    archetype, confidence, reason = "unknown", 0.0, "Not enough revealed moves to classify the style."
    if counts["recovery"] >= 4 and counts["status"] + counts["phazing"] >= 2 and turns >= 50:
        archetype, confidence, reason = "stall", .4, "Recovery on four Pokémon plus chip/phazing in a long game."
    elif counts["recovery"] >= 3 and counts["status"] + counts["phazing"] and turns >= 35:
        archetype, confidence, reason = "semi_stall", .35, "Several recovery and chip users revealed."
    elif counts["setup"] >= 3 or counts["setup"] >= 2 and counts["screens"]:
        archetype, confidence, reason = "hyper_offense", .35, "Several setup win conditions, sometimes backed by screens."
    elif counts["setup"] >= 2 and counts["recovery"]:
        archetype, confidence, reason = "bulky_offense", .3, "Offensive setup and sustain revealed on the same team."
    elif counts["recovery"] and counts["pivot"] and team["switches"] >= 5:
        archetype, confidence, reason = "balance", .25, "Recovery, pivots and frequent switching observed."
    return {"archetype": archetype, "confidence": confidence, "inferred": True, "reason": reason}


def parse_public_log(log: str) -> dict:
    """Parse only public battle protocol; identities remain local to parsing.

    Illusion's replace corrects the current record, and Transform/called moves
    are excluded from native move presence. Observed item/ability changes are
    retained as events, not assertions about the original set.
    """
    previews: dict[str, list[str]] = {"p1": [], "p2": []}
    entries: dict[tuple[str, str, str], dict] = {}
    active: dict[str, dict] = {}
    switches, forced, move_events = Counter(), Counter(), Counter()
    hazards, removals, phazing = Counter(), Counter(), Counter()
    lead_species: dict[str, str] = {}
    turns, finished, battle_timestamp = 0, False, None

    def actor(value: str) -> dict | None:
        return active.get(_slot(value))

    for line in log.splitlines()[:50_000]:
        if not line.startswith("|"):
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        event, args = parts[1], parts[2:]
        if event == "poke" and len(args) >= 2 and args[0] in previews:
            species = _species(args[1])
            if species:
                previews[args[0]].append(species)
        elif event in {"switch", "drag"} and len(args) >= 2:
            slot, species = _slot(args[0]), _species(args[1])
            if not slot or not species:
                continue
            side = slot[:2]
            nickname = args[0].split(":", 1)[1].strip()  # Never serialized.
            key = (side, nickname, species)
            entry = entries.setdefault(key, {"side": side, "species": species, "moves": set(), "items": set(),
                "abilities": set(), "tera_types": set(), "transformed": False})
            entry["transformed"] = False
            active[slot] = entry
            lead_species.setdefault(side, species)
            switches[side] += 1
            if event == "drag":
                forced[side] += 1
        elif event == "replace" and len(args) >= 2:
            slot = _slot(args[0])
            entry = active.get(slot)
            if entry:
                species = _species(args[1])
                if lead_species.get(slot[:2]) == entry["species"]:
                    lead_species[slot[:2]] = species
                entry["species"] = species
                entries[(slot[:2], args[0].split(":", 1)[1].strip(), species)] = entry
        elif event in {"detailschange", "-formechange"} and len(args) >= 2:
            entry = actor(args[0])
            if entry:
                entry["species"] = _species(args[1])
        elif event == "-transform":
            entry = actor(args[0])
            if entry:
                entry["transformed"] = True
        elif event == "move" and len(args) >= 2:
            entry, move = actor(args[0]), to_id(args[1])
            if not entry or not move:
                continue
            side = entry["side"]
            move_events[side] += 1
            if move in PHAZING:
                phazing[move] += 1
            called = any(tag.startswith("[from] move:") for tag in args[3:])
            if not entry["transformed"] and not called:
                entry["moves"].add(move)
        elif event == "turn":
            value = _number(args[0])
            if value is not None:
                turns = max(turns, min(10_000, int(value)))
        elif event == "t:":
            value = _number(args[0])
            if value is not None and value > 1_000_000_000:
                battle_timestamp = min(battle_timestamp, value) if battle_timestamp else value
        elif event in {"win", "tie"}:
            finished = True
        elif event in {"-sidestart", "-sideend"} and len(args) >= 2:
            hazard = to_id(args[1].removeprefix("move: "))
            if hazard in HAZARDS:
                (hazards if event == "-sidestart" else removals)[hazard] += 1
        # Event source tags disclose held items or abilities (including Rocky
        # Helmet/Intimidate belonging to an explicit [of] actor).
        if args and event.startswith("-"):
            entry = actor(args[0])
            if entry and event in {"-item", "-enditem", "-ability"} and len(args) >= 2:
                entry["abilities" if event == "-ability" else "items"].add(to_id(args[1]))
            if entry and event == "-terastallize" and len(args) >= 2:
                entry["tera_types"].add(to_id(args[1]))
            owner = next((actor(tag[5:]) for tag in args[1:] if tag.startswith("[of] ")), entry)
            if owner:
                for tag in args[1:]:
                    for prefix, field in (("[from] item: ", "items"), ("[from] ability: ", "abilities")):
                        if tag.startswith(prefix):
                            owner[field].add(to_id(tag[len(prefix):]))
                if event == "-activate" and len(args) >= 2:
                    for prefix, field in (("item: ", "items"), ("ability: ", "abilities")):
                        if args[1].startswith(prefix):
                            owner[field].add(to_id(args[1][len(prefix):]))
    # Multiple aliases may refer to the same record after Illusion breaks.
    unique = {id(entry): entry for entry in entries.values()}.values()
    teams = []
    for side in ("p1", "p2"):
        observed: dict[str, dict] = {}
        for entry in unique:
            if entry["side"] != side:
                continue
            mon = observed.setdefault(entry["species"], {"species": entry["species"], "moves": set(), "items": set(), "abilities": set(), "tera_types": set()})
            for field in ("moves", "items", "abilities", "tera_types"):
                mon[field].update(entry[field])
        revealed = [{**mon, **{field: sorted(mon[field]) for field in ("moves", "items", "abilities", "tera_types")},
                     "roles": sorted({role for move in mon["moves"] for role in _roles(move)})} for mon in observed.values()]
        roster = list(dict.fromkeys(previews[side] + list(observed)))
        # Preview hides several formes; a revealed forme refines that slot.
        roster = [name for name in roster if name in observed or not any(
            other != name and _base_species(other) == _base_species(name) and other in observed for other in roster)]
        team = {"species": roster[:6], "preview_complete": len(previews[side]) == 6,
                "revealed": revealed[:6], "lead_species": lead_species.get(side),
                "switches": max(0, switches[side] - 1), "forced_switches": forced[side], "move_events": move_events[side]}
        team["archetype"] = _archetype(team, turns)
        teams.append(team)
    return {"turns": turns, "finished": finished, "battle_timestamp": battle_timestamp, "teams": teams,
            "hazard_events": dict(hazards), "hazard_removal_events": dict(removals), "phazing_events": dict(phazing)}


def _summarize(format_id: str, replays: list[dict], min_rating: int, collected_at: str, warnings=()) -> dict:
    species, pairs, styles, style_confidence = {}, Counter(), Counter(), defaultdict(list)
    battles_per_species, moves_per_species = defaultdict(set), defaultdict(lambda: defaultdict(set))
    turns, hazard_events, removal_events, phazing_events = [], Counter(), Counter(), Counter()
    for replay in replays:
        identity = replay["id"]
        turns.append(replay["turns"])
        hazard_events.update(replay["hazard_events"])
        removal_events.update(replay["hazard_removal_events"])
        phazing_events.update(replay["phazing_events"])
        for team in replay["teams"]:
            styles[team["archetype"]["archetype"]] += 1
            style_confidence[team["archetype"]["archetype"]].append(team["archetype"]["confidence"])
            pairs.update(combinations(sorted(set(team["species"])), 2))
            revealed = {mon["species"]: mon for mon in team["revealed"]}
            for name in set(team["species"]):
                info = species.setdefault(name, {"teams": 0, "revealed_sets": 0, "moves": Counter(), "items": Counter(), "abilities": Counter(), "roles": Counter()})
                info["teams"] += 1
                battles_per_species[name].add(identity)
                if name in revealed:
                    info["revealed_sets"] += 1
                    for field in ("moves", "items", "abilities", "roles"):
                        info[field].update(set(revealed[name][field]))
                    for move in revealed[name]["moves"]:
                        moves_per_species[name][move].add(identity)
    teams = 2 * len(replays)
    for name, info in species.items():
        info["rate"] = round(info["teams"] / teams, 4) if teams else 0
        info["confidence"] = round(info["teams"] / (info["teams"] + 20), 4)
        for field in ("moves", "items", "abilities", "roles"):
            info[field] = dict(info[field])
    priors = {name: {"battles": len(seen), "confidence": round(len(seen) / (len(seen) + 20), 4),
                    "moves": {move: round(len(presence) / len(seen), 4) for move, presence in moves_per_species[name].items()}}
              for name, seen in battles_per_species.items() if moves_per_species[name]}
    notices = list(warnings)
    notices += ["A sample of selected public replays; it does not represent the whole ladder.",
                "The window uses the official upload date; when the log shows the battle date, that must be recent too.",
                "Unused moves stay unknown; revealed frequencies are lower bounds, not full sets.",
                "Items/abilities are observed events and may have changed during the battle. EVs, IVs, nature and hidden stats are not inferred.",
                "Styles are low-confidence guesses from revealed actions; no alternative play was simulated."]
    if len(replays) < 6:
        notices.append(f"Small sample: {len(replays)} recent replays at rating {min_rating}+; its influence should stay limited.")
    return {"schema": 1, "format_id": format_id, "collected_at": collected_at, "window_days": WINDOW_DAYS,
            "min_rating": min_rating, "sample_size": len(replays), "team_samples": teams, "fresh": bool(replays),
            "sources": [{key: replay[key] for key in ("id", "rating", "uploadtime", "uploaded_at", "source_url")} for replay in replays],
            "species": species, "pairs": [{"species": list(pair), "teams": count, "rate": round(count / teams, 4)} for pair, count in pairs.most_common(40)],
            "archetypes": {name: {"teams": count, "inferred": True, "confidence": round(sum(style_confidence[name]) / count, 4)} for name, count in styles.items()},
            "tempo": {"turns_mean": round(sum(turns) / len(turns), 2) if turns else None, "turns": turns,
                      "hazard_events": dict(hazard_events), "hazard_removal_events": dict(removal_events), "phazing_events": dict(phazing_events)},
            "observed_priors": {"version": 2, "format_id": format_id, "species": priors},
            "warnings": list(dict.fromkeys(notices)), "replays": replays}


class ReplayResearch:
    def __init__(self, base_dir: Path, timeout: float = 3.0):
        self.cache_dir = Path(base_dir) / "cache" / "replay_research"
        self.timeout = max(.1, float(timeout))
        self.last_error: str | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _check_format(format_id: str) -> None:
        if format_id not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {format_id}.")

    def _fetch(self, url: str, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ReplayResearchError("Replay research timed out.")
        request = urllib.request.Request(url, headers={"User-Agent": "Unaware/1.0 (public replay research)"})
        with urllib.request.urlopen(request, timeout=min(self.timeout, remaining)) as response:
            if urllib.parse.urlparse(response.geturl()).hostname != "replay.pokemonshowdown.com":
                raise ReplayResearchError("The replay redirected to a different origin.")
            body = bytearray()
            while len(body) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise ReplayResearchError("Replay research timed out.")
                chunk = response.read1(min(64 * 1024, MAX_BYTES + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
        if len(body) > MAX_BYTES:
            raise ReplayResearchError("Replay larger than the 4 MiB limit.")
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def _qualified(row: dict, format_id: str, min_rating: int, now: float) -> dict | None:
        identity = row.get("id", "")
        if not isinstance(identity, str) or not re.fullmatch(re.escape(format_id) + r"-\d+", identity):
            return None
        if row.get("private") not in (None, False, 0, "0") or row.get("password"):
            return None
        rating, uploaded = _number(row.get("rating")), _number(row.get("uploadtime"))
        if rating is None or rating < min_rating or uploaded is None or not now - WINDOW_DAYS * 86400 <= uploaded <= now + 3600:
            return None
        return {"id": identity, "rating": int(rating), "uploadtime": int(uploaded), "uploaded_at": _iso(uploaded), "source_url": BASE_URL + identity, "public": True}

    def load(self, format_id: str) -> dict | None:
        self._check_format(format_id)
        self.last_error = None
        try:
            raw = json.loads((self.cache_dir / f"{format_id}.json").read_text(encoding="utf-8"))
            if raw.get("schema") != 1 or raw.get("format_id") != format_id or not isinstance(raw.get("replays"), list):
                raise ValueError("incompatible cache format")
            threshold = int(raw["min_rating"])
            current = []
            for replay in raw["replays"][:MAX_REPLAYS]:
                metadata = self._qualified(replay, format_id, threshold, time.time())
                played = _number(replay.get("battle_timestamp"))
                recent_play = played is None or time.time() - WINDOW_DAYS * 86400 <= played <= time.time() + 3600
                if metadata and recent_play and replay.get("public") is True and replay.get("source_url") == metadata["source_url"] and isinstance(replay.get("teams"), list) and len(replay["teams"]) == 2:
                    current.append({**replay, **metadata})
            if not current:
                self.last_error = "The cache has no qualifying public replays in the current 120-day window."
                return None
            return _summarize(format_id, current, threshold, str(raw["collected_at"]), ["Local research cache; not refreshed yet this session."])
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            self.last_error = f"Invalid replay research cache: {type(exc).__name__}."
            return None

    def observed_priors(self, format_id: str) -> dict:
        snapshot = self.load(format_id)
        return snapshot["observed_priors"] if snapshot else {"version": 2, "format_id": format_id, "species": {}}

    def refresh(self, format_id: str, min_rating: int = 1700, limit: int = 6, max_duration: float = 10) -> dict:
        self._check_format(format_id)
        if isinstance(min_rating, bool) or not isinstance(min_rating, int) or min_rating < 0:
            raise ValueError("The minimum rating must be a non-negative integer.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_REPLAYS:
            raise ValueError(f"Limit must be between 1 and {MAX_REPLAYS}.")
        duration = _number(max_duration)
        if duration is None or duration <= 0:
            raise ValueError("The maximum duration must be positive.")
        if not self._lock.acquire(blocking=False):
            cached = self.load(format_id)
            return cached or _summarize(format_id, [], min_rating, _iso(time.time()), ["Research already running; no new sample was created."])
        try:
            return self._refresh(format_id, min_rating, limit, duration)
        finally:
            self._lock.release()

    def _refresh(self, format_id: str, min_rating: int, limit: int, max_duration: float) -> dict:
        deadline, now = time.monotonic() + max_duration, time.time()
        cached = self.load(format_id)
        self.last_error = None
        previous = {replay["id"]: replay for replay in (cached or {}).get("replays", []) if self._qualified(replay, format_id, min_rating, now)}
        candidates, blocked, warnings = {}, set(), []
        plans = [(page, None) for page in range(1, 4)] + [(1, "rating")]
        for page, sort in plans:
            if len(candidates) >= limit or time.monotonic() >= deadline:
                break
            query = {"format": format_id, "page": str(page)}
            if sort:
                query["sort"] = sort
            try:
                rows = json.loads(self._fetch(BASE_URL + "search.json?" + urllib.parse.urlencode(query), deadline))
                if not isinstance(rows, list):
                    raise ReplayResearchError("The replay index did not return a list.")
                for row in rows[:100]:
                    if not isinstance(row, dict):
                        continue
                    if not isinstance(row.get("id"), str):
                        continue
                    if row.get("private") not in (None, False, 0, "0") or row.get("password"):
                        blocked.add(row.get("id", ""))
                    metadata = self._qualified(row, format_id, min_rating, now)
                    if metadata:
                        candidates[metadata["id"]] = metadata
            except (urllib.error.URLError, TimeoutError, OSError, ValueError, ReplayResearchError) as exc:
                warnings.append(f"A replay page was unavailable ({type(exc).__name__}).")
        for identity in blocked:
            candidates.pop(identity, None)
            previous.pop(identity, None)
        chosen = sorted(candidates.values(), key=lambda row: (row["rating"], row["uploadtime"]), reverse=True)
        replays = []
        for metadata in chosen:
            if len(replays) >= limit or time.monotonic() >= deadline:
                break
            try:
                if metadata["id"] in previous:
                    parsed = previous[metadata["id"]]
                else:
                    parsed = parse_public_log(self._fetch(metadata["source_url"] + ".log", deadline))
                if not parsed.get("finished") or not all(team.get("species") for team in parsed["teams"]):
                    warnings.append("An incomplete replay was left out of the sample.")
                    continue
                played = _number(parsed.get("battle_timestamp"))
                if played is not None and not now - WINDOW_DAYS * 86400 <= played <= now + 3600:
                    warnings.append("A recently uploaded replay showed an old or invalid battle date and was excluded.")
                    continue
                replays.append({**parsed, **metadata})
            except (urllib.error.URLError, TimeoutError, OSError, ReplayResearchError) as exc:
                warnings.append(f"A public log was unavailable ({type(exc).__name__}).")
        if time.monotonic() >= deadline:
            warnings.append("Research hit its time limit; only finished downloads were used.")
        for identity, replay in previous.items():
            if len(replays) >= limit:
                break
            if identity not in {entry["id"] for entry in replays}:
                replays.append(replay)
                warnings.append("Part of the sample came from the public cache, still inside the 120-day window.")
        if not replays:
            self.last_error = f"No recent public replay at rating {min_rating}+ could be analysed."
            warnings.append(self.last_error)
        snapshot = _summarize(format_id, replays, min_rating, _iso(now), warnings)
        if replays:
            path = self.cache_dir / f"{format_id}.json"
            temporary = None
            try:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.cache_dir, suffix=".tmp", delete=False) as output:
                    temporary = Path(output.name)
                    json.dump(snapshot, output, ensure_ascii=False, indent=2)
                temporary.replace(path)
            except OSError:
                snapshot["warnings"].append("The sample was fetched but could not be written to the local cache.")
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return snapshot
