"""Build the team library shown in Teams > Library (assets/library.txt).

    .venv/Scripts/python tools/build_library.py

Only teams built by players:
- tools/library_seed.txt: hand-picked OU teams (tournament and ladder teams);
- tools/samples2026: the official Smogon SV OU sample teams;
- Smogon's sample teams for the other formats, from the pkmn data mirror
  (https://pkmn.github.io/smogon/data/teams/<format>.json).

Every team is checked by Pokémon Showdown's own validator (runtime/) and
dropped if it fails. Output blocks: "=== name | archetype | format | source".
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from unaware.strategy import analyze_team  # noqa: E402
from unaware.teams import parse_team  # noqa: E402

FORMATS = ("gen9ou", "gen9ubers", "gen9uu", "gen9ru", "gen9nu", "gen9pu", "gen9lc", "gen9monotype")
STAT = {"hp": "HP", "atk": "Atk", "def": "Def", "spa": "SpA", "spd": "SpD", "spe": "Spe"}
UA = {"User-Agent": "unaware-library-builder"}


def to_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def default_ability(species: str) -> str:
    """The pkmn mirror leaves the ability out when the species has only one."""
    from poke_env.data import GenData
    entry = GenData.from_gen(9).pokedex.get(to_id(species), {})
    return (entry.get("abilities") or {}).get("0", "")


def export(members: list[dict]) -> str:
    blocks = []
    for m in members:
        head = m["species"] + (f" ({m['gender']})" if m.get("gender") in ("M", "F") else "")
        lines = [head + (f" @ {m['item']}" if m.get("item") else "")]
        if m.get("ability"):
            lines.append(f"Ability: {m['ability']}")
        if m.get("level"):
            lines.append(f"Level: {m['level']}")
        if m.get("tera"):
            lines.append(f"Tera Type: {m['tera']}")
        evs = " / ".join(f"{v} {STAT[k]}" for k, v in (m.get("evs") or {}).items() if v)
        if evs:
            lines.append(f"EVs: {evs}")
        if m.get("nature"):
            lines.append(f"{m['nature']} Nature")
        ivs = " / ".join(f"{v} {STAT[k]}" for k, v in (m.get("ivs") or {}).items() if v != 31)
        if ivs:
            lines.append(f"IVs: {ivs}")
        lines += [f"- {move}" for move in m["moves"]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def mirror_teams(fmt: str, cache: Path) -> list[tuple[str, str]]:
    path = cache / f"teams_{fmt}.json"
    if not path.is_file():
        try:
            request = urllib.request.Request(f"https://pkmn.github.io/smogon/data/teams/{fmt}.json", headers=UA)
            with urllib.request.urlopen(request, timeout=60) as response:
                path.write_bytes(response.read())
        except Exception:
            return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    out = []
    for team in data:
        members = [{"species": s["species"], "gender": s.get("gender"), "item": s.get("item", ""),
                    "ability": s.get("ability") or default_ability(s["species"]), "level": s.get("level"),
                    "nature": s.get("nature", ""), "evs": s.get("evs") or {}, "ivs": s.get("ivs") or {},
                    "tera": s.get("teraType", ""), "moves": s.get("moves", [])[:4]} for s in team.get("data", [])]
        name = team.get("name", "Sample").strip() + (f" by {team['author']}" if team.get("author") else "")
        out.append((name, export(members)))
    return out


def validate(entries: list[tuple[str, str]]) -> list[list[str]]:
    """Pokémon Showdown's validator on (format, export) pairs; [] means legal."""
    script = """
const {Teams, TeamValidator} = require('./runtime/node_modules/pokemon-showdown');
let input = ''; process.stdin.on('data', d => input += d).on('end', () => {
  const out = JSON.parse(input).map(([format, text]) => {
    const team = Teams.import(text);
    if (!team || !team.length) return ['empty team'];
    return TeamValidator.get(format).validateTeam(team) || [];
  });
  process.stdout.write(JSON.stringify(out));
});"""
    result = subprocess.run([str(ROOT / "runtime" / "node.exe"), "-e", script], input=json.dumps(entries),
                            capture_output=True, text=True, encoding="utf-8", cwd=ROOT, check=True)
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=ROOT / "cache" / "library")
    parser.add_argument("--out", type=Path, default=ROOT / "assets" / "library.txt")
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str, str]] = []  # name, format, export
    seed = ROOT / "tools" / "library_seed.txt"
    if seed.is_file():
        for block in seed.read_text(encoding="utf-8").split("=== ")[1:]:
            header, _, text = block.partition("\n")
            rows.append((header.split("|")[0].strip(), "gen9ou", text.strip() + "\n"))
    for path in sorted((ROOT / "tools" / "samples2026").glob("*.txt")):
        rows.append((path.stem, "gen9ou", path.read_text(encoding="utf-8").strip() + "\n"))
    for fmt in FORMATS:
        rows += [(name, fmt, text) for name, text in mirror_teams(fmt, args.cache)]
    problems = validate([(fmt, text) for _, fmt, text in rows])
    kept, seen, dropped = [], set(), 0
    for (name, fmt, text), errors in zip(rows, problems):
        try:
            members = parse_team(text)
        except ValueError:
            errors = errors or ["unparsable"]
        if errors:
            dropped += 1
            continue
        key = (fmt, tuple(sorted(to_id(m.species) for m in members)))
        if key in seen:
            continue
        seen.add(key)
        kept.append(f"=== {name} | {analyze_team(members)['archetype']} | {fmt} | sample\n{text.strip()}\n")
    args.out.write_text("\n".join(kept), encoding="utf-8")
    print(f"{len(kept)} teams written to {args.out} ({dropped} failed validation)", flush=True)


if __name__ == "__main__":
    main()
