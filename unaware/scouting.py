"""Opponent scouting: what this player brought and did in earlier games.

When a battle starts, the bot reads the opponent's recent public replays on
the official replay server plus every local game against them, and builds a
profile: usual teams, leads, moves/items/abilities/Tera per Pokémon, style
(HO, stall...), switching tendency and our record against them.

The profile is a prior. Revealed information in the current battle always wins;
a player can bring a different team or set. Network access happens on a
background thread, never on the decision path, with a strict deadline.

Once the team preview is known, for_team narrows the profile to the games
with this same team (SAME_TEAM of the 6 species): style, leads and switching
come only from those, so an old team's lead or style is never shown for a new
one (15/10: "lead Clefable" against a team without Clefable). Our own saved
replays count as full games (lead, sets, switches), not just a species list.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import html
import json
from pathlib import Path
import re
import threading
import time
import urllib.parse

from .replay_research import BASE_URL, ReplayResearch, parse_public_log
from .teams import to_id

CACHE_HOURS = 6
SAME_TEAM = 4  # species shared with the preview for a game to count as this team
LABELS = {"hyper_offense": "Hyper Offense", "bulky_offense": "Bulky Offense", "balance": "Balance",
          "semi_stall": "Semi-stall", "stall": "Stall", "weather": "Weather", "unknown": "unknown"}


def _base(species: str) -> str:
    from .strategy import base_species_id
    try:
        return base_species_id(species)
    except Exception:
        return to_id(species)


PRIORITY = {"extremespeed", "suckerpunch", "aquajet", "bulletpunch", "iceshard", "machpunch", "shadowsneak",
            "quickattack", "vacuumwave", "jetpunch", "firstimpression", "accelerock", "grassyglide", "thunderclap",
            "upperhand", "fakeout", "feint"}


def infer_hidden(log: str, side: int) -> dict:
    """Items/speed this player's Pokémon showed by behaviour, not by reveal.

    - Heavy-Duty Boots: switched into Stealth Rock/Spikes and took no damage.
    - Choice item: repeated one attack 3+ turns in a row without switching.
    - Choice Scarf / max Speed: moved first (no priority) against a Pokémon
      with a higher base Speed, outside Trick Room and paralysis.
    Returns {species: {"items": {...}, "fast": bool}}.
    """
    from poke_env.data import GenData
    dex = GenData.from_gen(9).pokedex
    me, them = f"p{side + 1}", f"p{2 - side}"
    hazards = {me: set(), them: set()}
    active: dict[str, str] = {}
    pending_switch: dict[str, str] = {}
    streak: dict[str, tuple[str, int]] = {}
    statuses: dict[str, str] = {}
    trick_room = False
    speed_noise = False  # weather/Tailwind/boosts/drops make move order unreliable
    not_choice: set[str] = set()  # used a status move or showed a non-choice item
    turn_moves: list[tuple[str, str]] = []
    result: dict[str, dict] = {}

    def entry(species: str) -> dict:
        return result.setdefault(_base(species), {"items": {}, "fast": False})

    def base_speed(species: str) -> int:
        return dex.get(to_id(species), {}).get("baseStats", {}).get("spe", 0)

    def close_turn():
        # The first non-priority mover vs a faster species outspeeds it.
        if len(turn_moves) == 2 and not trick_room and not speed_noise:
            (first_side, first_move), (second_side, _) = turn_moves
            if first_side == me and second_side == them and first_move not in PRIORITY:
                mine, theirs = active.get(me), active.get(them)
                if mine and theirs and statuses.get(theirs) != "par" and base_speed(mine) < base_speed(theirs):
                    entry(mine)["fast"] = True
        turn_moves.clear()

    for line in log.splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        kind = parts[1]
        if kind == "turn":
            close_turn()
            speed_noise = False
            for slot, species in list(pending_switch.items()):
                if slot == me and hazards[me] & {"stealthrock", "spikes"}:
                    data = entry(species)["items"]
                    data["heavydutyboots"] = data.get("heavydutyboots", 0) + 1
            pending_switch.clear()
        elif kind in {"switch", "drag"} and len(parts) > 3:
            slot, species = parts[2][:2], parts[3].split(",")[0]
            active[slot] = species
            streak.pop(slot, None)
            if slot == me:
                pending_switch[slot] = species
        elif kind == "-damage" and len(parts) > 4 and parts[2][:2] == me and any(
                tag.startswith("[from] Stealth Rock") or tag.startswith("[from] Spikes") for tag in parts[4:]):
            pending_switch.pop(me, None)
        elif kind == "-sidestart" and len(parts) > 3:
            owner = parts[2][:2]
            hazards[owner].add(to_id(parts[3].replace("move: ", "")))
        elif kind == "-sideend" and len(parts) > 3:
            hazards[parts[2][:2]].discard(to_id(parts[3].replace("move: ", "")))
        elif kind in {"-weather", "-boost", "-unboost", "-setboost", "-clearallboost"} or (
                kind == "-sidestart" and ("Tailwind" in line or "Sticky Web" in line)):
            if kind != "-weather" or (len(parts) > 2 and parts[2] != "none"):
                speed_noise = True
            if kind == "-sidestart" and len(parts) > 3:
                hazards[parts[2][:2]].add(to_id(parts[3].replace("move: ", "")))
        elif kind == "-heal" and len(parts) > 4 and parts[2][:2] == me and "[from] item:" in line:
            not_choice.add(_base(active.get(me, "")))
        elif kind == "-status" and len(parts) > 3:
            statuses[active.get(parts[2][:2], "")] = parts[3]
        elif kind == "-fieldstart" and "Trick Room" in line:
            trick_room = True
        elif kind == "-fieldend" and "Trick Room" in line:
            trick_room = False
        elif kind == "move" and len(parts) > 3 and "[from]" not in line:
            slot, move = parts[2][:2], to_id(parts[3])
            turn_moves.append((slot, move))
            if slot == me:
                previous, count = streak.get(slot, ("", 0))
                count = count + 1 if previous == move else 1
                streak[slot] = (move, count)
                data = GenData.from_gen(9).moves.get(move, {})
                if data.get("category") == "Status" and active.get(me):
                    not_choice.add(_base(active[me]))
                if count >= 3 and data.get("category") in {"Physical", "Special"} and active.get(me):
                    items = entry(active[me])["items"]
                    choice = "choicespecs" if data.get("category") == "Special" else "choiceband"
                    items[choice] = items.get(choice, 0) + 1
    for species in not_choice:
        if species in result:
            for item in ("choiceband", "choicespecs", "choicescarf"):
                result[species]["items"].pop(item, None)
    for species, data in result.items():
        if data["fast"] and not data["items"] and species not in not_choice:
            data["items"]["choicescarf"] = data["items"].get("choicescarf", 0)  # possible, weaker than a reveal
    return result


class OpponentScout:
    def __init__(self, base_dir: Path, experience=None, timeout: float = 3.0):
        self.base_dir = Path(base_dir)
        self.experience = experience
        self.research = ReplayResearch(self.base_dir, timeout=timeout)
        self.cache_dir = self.base_dir / "cache" / "scouting"
        self._lock = threading.Lock()

    # ---- public replays -------------------------------------------------
    def _replays(self, username: str, format_id: str, limit: int, deadline: float) -> list[dict]:
        user = to_id(username)
        query = urllib.parse.urlencode({"user": user, "format": format_id})
        rows = json.loads(self.research._fetch(BASE_URL + "search.json?" + query, deadline))
        games = []
        for row in rows[:limit]:
            if time.monotonic() >= deadline - .3:
                break
            identity = str(row.get("id", ""))
            if not re.fullmatch(r"[a-z0-9-]+", identity) or row.get("private"):
                continue
            try:
                data = json.loads(self.research._fetch(BASE_URL + identity + ".json", deadline))
            except Exception:
                continue
            players = [to_id(name) for name in data.get("players", [])]
            if user not in players:
                continue
            side = players.index(user)
            log = data.get("log", "")
            parsed = parse_public_log(log)
            winner = re.search(r"^\|win\|(.+)$", log, re.M)
            team = parsed["teams"][side]
            team["inferred"] = infer_hidden(log, side)
            games.append({"id": identity, "rating": row.get("rating"), "uploadtime": row.get("uploadtime"),
                          "turns": parsed["turns"], "team": team,
                          "won": bool(winner and to_id(winner[1]) == user),
                          "source": "replay"})
        return games

    # ---- local history ---------------------------------------------------
    def _local(self, username: str, format_id: str) -> list[dict]:
        if self.experience is None:
            return []
        rows = []
        try:
            history = self.experience.history(2000)
        except Exception:
            return []
        for record in history:
            if to_id(record.get("opponent", "")) != to_id(username) or record.get("format_id") != format_id:
                continue
            team = self._local_team(record.get("replay_path"), username) or {
                "species": [to_id(name) for name in record.get("opponent_pokemon") or []],
                "revealed": [], "lead_species": None, "switches": 0, "archetype": {"archetype": "unknown"}}
            rows.append({"id": record.get("battle_tag") or record.get("battle_id"), "turns": record.get("turns") or 0,
                         "team": team, "won": record.get("result") == "loss", "our_result": record.get("result"),
                         "source": "local"})
        return rows

    @staticmethod
    def _local_team(replay_path, username: str) -> dict | None:
        """The opponent's side of our saved replay: preview, lead, revealed sets,
        switches and inferred items, as for a public replay."""
        try:
            text = Path(replay_path).read_text(encoding="utf-8")
        except (OSError, TypeError, ValueError):
            return None
        match = re.search(r'class="battle-log-data">(.*?)</script>', text, re.S)
        if not match:
            return None
        log = html.unescape(match.group(1)).replace("<\\/", "</")
        players = dict(re.findall(r"^\|player\|(p[12])\|([^|\n]+)", log, re.M))
        side = next((int(slot[1]) - 1 for slot, name in players.items() if to_id(name) == to_id(username)), None)
        if side is None:
            return None
        team = parse_public_log(log)["teams"][side]
        team["inferred"] = infer_hidden(log, side)
        return team

    # ---- profile ----------------------------------------------------------
    def _cache_path(self, username: str, format_id: str) -> Path:
        return self.cache_dir / f"{format_id}-{to_id(username)}.json"

    def scout(self, username: str, format_id: str, limit: int = 10, max_duration: float = 7.) -> dict:
        if not username:
            return {}
        path = self._cache_path(username, format_id)
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - cached.get("collected_at", 0) < CACHE_HOURS * 3600:
                games = _merge(cached.get("games", []), self._local(username, format_id))
                return {**self.profile(username, format_id, games, cached.get("warnings", [])), "raw_games": games}
        except (OSError, ValueError):
            pass
        deadline = time.monotonic() + max_duration
        warnings = []
        try:
            games = self._replays(username, format_id, limit, deadline)
        except Exception as exc:
            games, warnings = [], [f"Public replays unavailable: {type(exc).__name__}."]
        try:
            with self._lock:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"collected_at": time.time(), "games": games, "warnings": warnings}), encoding="utf-8")
        except OSError:
            pass
        games = _merge(games, self._local(username, format_id))
        # raw_games: the per-game teams, for Foul Play's set prior (fp_scouting).
        return {**self.profile(username, format_id, games, warnings), "raw_games": games}

    @staticmethod
    def for_team(profile: dict, preview_species) -> dict:
        """The profile narrowed to this battle's team (see the module doc). Sets
        per species come from every game but only for the species in this
        preview; style, leads, switching and length only from games with this
        same team; our record against the player from every game."""
        games = profile.get("raw_games")
        preview = {_base(name) for name in preview_species or []}
        if games is None or not preview:
            return profile
        same = [g for g in games
                if len({_base(n) for n in (g.get("team") or {}).get("species", [])} & preview) >= SAME_TEAM]
        username, format_id = profile.get("username", ""), profile.get("format_id", "")
        overall = OpponentScout.profile(username, format_id, games, profile.get("warnings", ()))
        view = OpponentScout.profile(username, format_id, same, profile.get("warnings", ())) if same else {}
        species = {name: data for name, data in (overall.get("species") or {}).items() if name in preview}
        species.update({name: data for name, data in (view.get("species") or {}).items() if name in preview})
        leads = {name: share for name, share in (view.get("leads") or {}).items() if name in preview}
        record = overall.get("record") or {}
        local = sum(1 for g in games if g.get("source") == "local")
        parts = []
        if same:
            parts.append(f"this team in {len(same)} of {len(games)} games")
            if view.get("archetype", "unknown") != "unknown":
                parts.append(f"style {LABELS.get(view['archetype'], view['archetype'])}")
            if leads:
                top = max(leads, key=leads.get)
                parts.append(f"lead {top} ({round(100 * leads[top])}%)")
            rate = view.get("switch_rate")
            if rate is not None:
                parts.append("switches a lot" if rate > .55 else "rarely switches" if rate < .25 else "switches moderately")
        elif games:
            parts.append(f"new team ({len(games)} games with other teams)")
        known = [name for name, data in species.items() if data.get("moves") or data.get("items")]
        if known:
            parts.append("known sets: " + ", ".join(known[:6]))
        if local:
            parts.append(f"vs us: {record.get('win', 0)}W {record.get('loss', 0)}L")
        summary = "; ".join(parts) + "." if parts else "No public or local history for this player."
        return {**profile, "games": len(games), "team_games": len(same), "species": species, "leads": leads,
                "archetype": view.get("archetype", "unknown") if same else "unknown",
                "archetype_confidence": view.get("archetype_confidence", 0) if same else 0,
                "common_team": sorted(preview), "team_repeat": round(len(same) / max(1, len(games)), 3),
                "switch_rate": view.get("switch_rate") if same else overall.get("switch_rate"),
                "average_turns": view.get("average_turns") if same else None,
                "record": record, "local_games": local, "summary": summary, "team_view": True}

    @staticmethod
    def profile(username: str, format_id: str, games: list[dict], warnings=()) -> dict:
        species_seen, leads, teams, styles = Counter(), Counter(), Counter(), Counter()
        details: dict[str, dict] = defaultdict(lambda: {"games": 0, "moves": Counter(), "items": Counter(),
                                                         "abilities": Counter(), "tera": Counter()})
        switches = turns = replays = 0
        inferred_items: dict[str, Counter] = defaultdict(Counter)
        fast: Counter = Counter()
        record = Counter()
        for game in games:
            team = game.get("team") or {}
            roster = [_base(name) for name in team.get("species", [])]
            for name in set(roster):
                species_seen[name] += 1
            if roster:
                teams[tuple(sorted(set(roster)))] += 1
            if team.get("lead_species"):
                leads[_base(team["lead_species"])] += 1
            style = (team.get("archetype") or {}).get("archetype", "unknown")
            if style != "unknown":
                styles[style] += 1
            for mon in team.get("revealed", []):
                entry = details[_base(mon["species"])]
                entry["games"] += 1
                for field, key in (("moves", "moves"), ("items", "items"), ("abilities", "abilities"), ("tera_types", "tera")):
                    for value in mon.get(field, []):
                        entry[key][value] += 1
            for species, data in (team.get("inferred") or {}).items():
                for item, count in data.get("items", {}).items():
                    inferred_items[species][item] += max(1, count) if count else 0
                fast[species] += bool(data.get("fast"))
            if game.get("source") == "replay":
                replays += 1
                switches += team.get("switches", 0)
                turns += max(1, game.get("turns") or 1)
            if game.get("our_result"):
                record[game["our_result"]] += 1
        total = len(games)
        if not total:
            return {"username": username, "format_id": format_id, "games": 0, "species": {}, "leads": {},
                    "summary": "No public or local history for this player.", "warnings": list(warnings)}
        def freq(counter, base):
            return {key: round(value / max(1, base), 3) for key, value in counter.most_common(8)}
        species = {name: {"seen": round(species_seen[name] / total, 3),
                          "moves": freq(details[name]["moves"], details[name]["games"]),
                          "items": freq(details[name]["items"], details[name]["games"]),
                          "abilities": freq(details[name]["abilities"], details[name]["games"]),
                          "tera": freq(details[name]["tera"], details[name]["games"]),
                          "inferred_items": dict(inferred_items[name].most_common(3)),
                          "fast": round(fast[name] / max(1, species_seen[name]), 3)}
                   for name in species_seen}
        archetype, votes = (styles.most_common(1)[0] if styles else ("unknown", 0))
        common_team = list(teams.most_common(1)[0][0]) if teams else []
        lead_total = sum(leads.values())
        switch_rate = round(switches / turns, 3) if turns else None
        top_lead = leads.most_common(1)[0][0] if leads else ""
        parts = [f"{total} games analysed ({replays} public replays)"]
        if archetype != "unknown":
            parts.append(f"style {LABELS.get(archetype, archetype)}")
        if top_lead:
            parts.append(f"most common lead: {top_lead} ({leads[top_lead]}/{lead_total})")
        if switch_rate is not None:
            parts.append("switches a lot" if switch_rate > .55 else "rarely switches" if switch_rate < .25 else "switches moderately")
        if record:
            parts.append(f"our record: {record['win']}W {record['loss']}L")
        return {"username": username, "format_id": format_id, "games": total, "replays": replays,
                "archetype": archetype, "archetype_confidence": round(min(.7, votes / max(1, replays) * min(1, replays / 4)), 3),
                "leads": {key: round(value / max(1, lead_total), 3) for key, value in leads.most_common()},
                "common_team": common_team, "team_repeat": round(teams.most_common(1)[0][1] / total, 3) if teams else 0,
                "species": species, "switch_rate": switch_rate,
                "average_turns": round(turns / replays, 1) if replays else None,
                "record": dict(record), "summary": "; ".join(parts) + ".", "warnings": list(warnings)}


def scouted_set(profile: dict, species: str) -> dict:
    """Moves/item/ability/Tera this player has shown on a species before."""
    entry = (profile or {}).get("species", {}).get(_base(species))
    if not entry:
        return {}
    pick = lambda values: max(values, key=values.get) if values else ""
    item = pick(entry.get("items", {})) or pick({k: v for k, v in entry.get("inferred_items", {}).items() if v})
    if not item and entry.get("fast", 0) >= .5:
        item = "choicescarf"
    return {"moves": [move for move, share in sorted(entry.get("moves", {}).items(), key=lambda kv: -kv[1]) if share >= .25][:4],
            "item": item, "ability": pick(entry.get("abilities", {})),
            "tera": pick(entry.get("tera", {})), "fast": entry.get("fast", 0) >= .5}


def _merge(public: list[dict], local: list[dict]) -> list[dict]:
    """Public replays plus our own games, without counting a game twice (a
    game we played may also be on the replay server)."""
    def number(identity) -> str:
        match = re.search(r"(\d{6,})", str(identity or ""))
        return match.group(1) if match else str(identity)
    ours = {number(game.get("id")) for game in local}
    return [game for game in public if number(game.get("id")) not in ours] + local
