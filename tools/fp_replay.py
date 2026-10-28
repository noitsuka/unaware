"""Replay a saved battle through Foul Play, turn by turn.

For each turn of a replay in replays/*.html this feeds Foul Play the same
protocol the live bridge fed it, plus a request rebuilt from our team and the
HP/status/item the log shows for our side, and prints what Foul Play would
choose next to what we actually played. Used to diagnose ladder losses.

    python tools/fp_replay.py replays/battle-gen9ou-123.html --turns 10-16
    python tools/fp_replay.py <replay> --search-ms 2000 --parallelism 4 --log fp.log

Positions are reconstructed from our view of the log: PP and hidden
information on our side that the log does not show (Protect counters, exact
PP) can differ from the live battle.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

NATURES = {
    "adamant": ("atk", "spa"), "bold": ("def", "atk"), "brave": ("atk", "spe"), "calm": ("spd", "atk"),
    "careful": ("spd", "spa"), "gentle": ("spd", "def"), "hasty": ("spe", "def"), "impish": ("def", "spa"),
    "jolly": ("spe", "spa"), "lax": ("def", "spd"), "lonely": ("atk", "def"), "mild": ("spa", "def"),
    "modest": ("spa", "atk"), "naive": ("spe", "spd"), "naughty": ("atk", "spd"), "quiet": ("spa", "spe"),
    "rash": ("spa", "spd"), "relaxed": ("def", "spe"), "sassy": ("spd", "spe"), "timid": ("spe", "atk"),
}
STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def parse_team(text: str) -> list[dict]:
    mons = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        head = lines[0]
        name, _, item = head.partition(" @ ")
        gender = ""
        g = re.search(r"\((M|F)\)\s*$", name)
        if g:
            gender, name = g.group(1), name[:g.start()].strip()
        species = re.sub(r"\s*\(.*\)$", "", name).strip()
        mon = {"species": species, "gender": gender, "item": norm(item), "ability": "", "tera": "",
               "evs": dict.fromkeys(STAT_KEYS, 0), "ivs": dict.fromkeys(STAT_KEYS, 31), "nature": "serious",
               "moves": []}
        for line in lines[1:]:
            if line.startswith("Ability:"):
                mon["ability"] = norm(line.split(":", 1)[1])
            elif line.startswith("Tera Type:"):
                mon["tera"] = line.split(":", 1)[1].strip()
            elif line.startswith(("EVs:", "IVs:")):
                key = "evs" if line.startswith("EVs") else "ivs"
                for part in line.split(":", 1)[1].split("/"):
                    value, stat = part.split()
                    mon[key][norm(stat)] = int(value)
            elif line.endswith("Nature"):
                mon["nature"] = norm(line.split()[0])
            elif line.startswith("- "):
                mon["moves"].append(norm(line[2:]))
        mons.append(mon)
    return mons


def stats_for(mon: dict) -> dict:
    from fp.data import pokedex
    names = {"hp": "hp", "atk": "attack", "def": "defense", "spa": "special-attack",
             "spd": "special-defense", "spe": "speed"}
    raw = pokedex[norm(mon["species"])]["baseStats"]
    base = {k: raw[v] for k, v in names.items()}
    plus, minus = NATURES.get(mon["nature"], (None, None))
    out = {}
    for key in STAT_KEYS:
        core = (2 * base[key] + mon["ivs"][key] + mon["evs"][key] // 4)
        if key == "hp":
            out[key] = core + 110
        else:
            value = core + 5
            mult = 1.1 if key == plus else .9 if key == minus else 1
            out[key] = math.floor(value * mult)
    return out


def battle_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'class="battle-log-data">(.*?)</script>', text, re.S)
    skip = ("|j|", "|l|", "|c|", "|raw|", "|chat|", "|html|", "|uhtml|", "|n|")
    return [l for l in (match.group(1) if match else text).split("\n")
            if l.startswith("|") and not l.startswith(skip)]


class OurSide:
    def __init__(self, team: list[dict], side: str, username: str):
        self.team, self.side, self.username = team, side, username
        for mon in team:
            mon["stats"] = stats_for(mon)
            mon["hp"], mon["status"], mon["held"] = mon["stats"]["hp"], "", mon["item"]
        self.active = None
        self.tera_used = False
        self.rqid = 0

    def find(self, species_or_details: str) -> dict:
        key = norm(species_or_details.split(",")[0])
        for mon in self.team:
            if norm(mon["species"]) == key or key.startswith(norm(mon["species"])) or norm(mon["species"]).startswith(key):
                return mon
        raise KeyError(species_or_details)

    def by_ident(self, ident: str) -> dict:
        nick = ident.split(": ", 1)[1]
        return self.find(nick)

    def observe(self, line: str) -> None:
        p = line.split("|")
        if len(p) < 3 or not p[2].startswith(self.side):
            return
        kind = p[1]
        if kind in ("switch", "drag", "replace"):
            mon = self.find(p[3])
            self.active = mon
            self._condition(mon, p[4])
        elif kind in ("-damage", "-heal", "-sethp"):
            self._condition(self.by_ident(p[2]), p[3])
        elif kind == "faint":
            self.by_ident(p[2])["hp"] = 0
        elif kind == "-status":
            self.by_ident(p[2])["status"] = p[3]
        elif kind == "-curestatus":
            self.by_ident(p[2])["status"] = ""
        elif kind == "-enditem":
            self.by_ident(p[2])["held"] = ""
        elif kind == "-item":
            self.by_ident(p[2])["held"] = norm(p[3])
        elif kind == "-terastallize":
            self.tera_used = True

    @staticmethod
    def _condition(mon: dict, cond: str) -> None:
        cond = cond.strip()
        if cond.startswith("0"):
            mon["hp"] = 0
            return
        hp, _, status = cond.partition(" ")
        mon["hp"] = int(hp.split("/")[0])
        mon["status"] = status if status != "fnt" else ""

    def request(self, force_switch: bool = False, team_preview: bool = False) -> dict:
        self.rqid += 1
        order = list(self.team)
        if self.active and not team_preview:
            order.remove(self.active)
            order.insert(0, self.active)
        pokemon = []
        for mon in order:
            details = mon["species"] + (f", {mon['gender']}" if mon["gender"] else "")
            hp = mon["hp"]
            cond = "0 fnt" if hp <= 0 else f"{hp}/{mon['stats']['hp']}" + (f" {mon['status']}" if mon["status"] else "")
            pokemon.append({
                "ident": f"{self.side}: {mon['species']}", "details": details, "condition": cond,
                # Showdown marks the first slot active during team preview too.
                "active": (mon is order[0]) if team_preview else (mon is self.active),
                "stats": {k: mon["stats"][k] for k in ("atk", "def", "spa", "spd", "spe")},
                "moves": mon["moves"], "baseAbility": mon["ability"], "ability": mon["ability"],
                "item": mon["held"], "pokeball": "pokeball", "teraType": mon["tera"], "terastallized": "",
            })
        req = {"side": {"name": self.username, "id": self.side, "pokemon": pokemon}, "rqid": self.rqid}
        if team_preview:
            req["teamPreview"] = True
        elif force_switch:
            req["forceSwitch"] = [True]
        else:
            active = {"moves": [{"move": m, "id": m, "pp": 16, "maxpp": 16, "target": "normal", "disabled": False}
                                for m in self.active["moves"]]}
            if not self.tera_used:
                active["canTerastallize"] = self.active["tera"]
            req["active"] = [active]
        return req


def our_action(turn_lines: list[str], side: str) -> str:
    """What we did in a turn: our first non-forced switch or move."""
    for line in turn_lines:
        p = line.split("|")
        if len(p) < 3 or not p[2].startswith(side):
            continue
        if p[1] == "switch":
            return "switch " + norm(p[3].split(",")[0])
        if p[1] == "move" and "[from]" not in line:
            tera = any(l.startswith(f"|-terastallize|{side}") for l in turn_lines)
            return norm(p[3]) + ("-tera" if tera else "")
    return "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("replay")
    ap.add_argument("--team", default="")
    ap.add_argument("--user", default="ritleriano")
    ap.add_argument("--turns", default="", help="e.g. 10-16 (default: all)")
    ap.add_argument("--search-ms", type=int, default=2000)
    ap.add_argument("--parallelism", type=int, default=4)
    ap.add_argument("--repeat", type=int, default=1, help="ask Foul Play N times per turn")
    ap.add_argument("--log", default="", help="Foul Play debug log file")
    ap.add_argument("--scouting", action="store_true", help="feed cache/scouting for the opponent (set prior)")
    ap.add_argument("--engine", default="foulplay", choices=("foulplay", "hybrid", "neural"), help="who picks the move")
    args = ap.parse_args()
    if args.log:
        os.environ["SHOWDOWN_FP_LOG"] = args.log

    from unaware import foulplay_brain
    foulplay_brain.configure(args.user, "gen9ou", args.search_ms, args.parallelism)

    if not args.team:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        args.team = str(ROOT / "teams" / cfg["selected_team"])
    team_text = Path(args.team).read_text(encoding="utf-8")

    lines = battle_lines(Path(args.replay))
    side = next(l.split("|")[2] for l in lines if l.startswith("|player|") and norm(l.split("|")[3]) == norm(args.user))
    opp_name = next(l.split("|")[3] for l in lines if l.startswith("|player|") and l.split("|")[2] != side)
    tag = "battle-replay-1"
    lo, hi = 0, 10 ** 6
    if args.turns:
        a, _, b = args.turns.partition("-")
        lo, hi = int(a), int(b or a)

    ours = OurSide(parse_team(team_text), side, args.user)
    bridge = foulplay_brain.FoulPlayBattle(tag, args.user, "gen9ou", team_text, args.search_ms, args.parallelism,
                                           engine=args.engine)
    if args.scouting:
        cache = ROOT / "cache" / "scouting" / f"gen9ou-{norm(opp_name)}.json"
        number = re.search(r"gen9ou-(\d+)", Path(args.replay).name)
        games = json.loads(cache.read_text(encoding="utf-8")).get("games", []) if cache.exists() else []
        games = [g for g in games if not number or g.get("id") != f"gen9ou-{number.group(1)}"]
        bridge.set_scouting(games)
        print(f"scouting: {len(games)} games")

    def feed(chunk: list[str]) -> None:
        bridge.feed(f">{tag}\n" + "\n".join(chunk))

    def ask(req: dict) -> str | None:
        bridge.feed(f">{tag}\n|request|{json.dumps(req)}")
        return bridge.decision(str(req["rqid"]), 120)

    feed(["|init|battle", f"|title|{args.user} vs. {opp_name}"])
    preview_end = next(i for i, l in enumerate(lines) if l.startswith("|teampreview"))
    head = [l for l in lines[:preview_end + 1] if not l.startswith(("|init", "|title"))]
    # Showdown sends each |player| line as its own message; Foul Play takes the
    # side id from the first one that names the opponent.
    for line in head:
        if line.startswith("|player|"):
            feed([line])
    feed([l for l in head if not l.startswith("|player|")])
    lead = ask(ours.request(team_preview=True))
    print(f"team preview: Foul Play -> {lead}")

    # What happened in each turn: the lines after its |turn| marker.
    turn = 0
    turn_logs: dict[int, list[str]] = {}
    for line in lines[preview_end + 1:]:
        if line.startswith("|turn|"):
            turn = int(line.split("|")[2])
        turn_logs.setdefault(turn, []).append(line)
    turn = 0
    pending: list[str] = []
    for line in lines[preview_end + 1:]:
        pending.append(line)
        ours.observe(line)
        if line.startswith("|turn|"):
            turn = int(line.split("|")[2])
            feed(pending)
            pending = []
            if turn > hi:
                break
            if turn < lo:
                # No request: Foul Play keeps the lines and applies them all at
                # the next request, so earlier turns cost no search.
                continue
            actual = our_action(turn_logs.get(turn, []), side)
            answers = [ask(ours.request()) for _ in range(args.repeat)]
            act = ours.active
            print(f"T{turn:>3} {act['species']:<16} {act['hp']:>3}/{act['stats']['hp']:<3} "
                  f"played={actual:<22} foulplay={answers}")
    bridge.close()


if __name__ == "__main__":
    main()
