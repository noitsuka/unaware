"""Local team tournament: the bot pilots both sides on a local Showdown server.

Usage (server started with `node pokemon-showdown start --no-security 8000`):
    .venv/Scripts/python tools/arena.py --games 2 --parallel 4 --budget 1500 --out arena.json

Every team plays every other team (both sides piloted by the same bot), so the
ranking reflects team strength under the bot's play style, not ladder Elo.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poke_env.ps_client import AccountConfiguration  # noqa: E402
from poke_env.ps_client.server_configuration import LocalhostServerConfiguration  # noqa: E402

from unaware.engine import DecisionEngine  # noqa: E402
from unaware.runner import StrategicPlayer  # noqa: E402
from unaware.search import SimulatorSearch  # noqa: E402
from unaware.meta import MetaStore  # noqa: E402

STATS = {"hp": "HP", "atk": "Atk", "def": "Def", "spa": "SpA", "spd": "SpD", "spe": "Spe"}


def export(team: list[dict]) -> str:
    blocks = []
    for mon in team:
        lines = [mon["species"] + (f" @ {mon['item']}" if mon.get("item") else "")]
        if mon.get("ability"):
            lines.append(f"Ability: {mon['ability']}")
        if mon.get("teraType"):
            lines.append(f"Tera Type: {mon['teraType']}")
        if mon.get("evs"):
            lines.append("EVs: " + " / ".join(f"{v} {STATS[k]}" for k, v in mon["evs"].items() if v))
        if mon.get("nature"):
            lines.append(f"{mon['nature']} Nature")
        if mon.get("ivs"):
            lines.append("IVs: " + " / ".join(f"{v} {STATS[k]}" for k, v in mon["ivs"].items()))
        lines += [f"- {move}" for move in mon["moves"]]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def load_teams(extra_dir: Path | None, validator: SimulatorSearch) -> dict[str, dict]:
    catalog = json.loads((ROOT / "assets/opponent_sets/gen9ou.json").read_text(encoding="utf-8"))
    teams = {}
    for team in catalog.get("teams", []):
        teams[team.get("name") or "Tusk Gliscor balance"] = {"text": export(team["data"]), "archetype": "auto", "source": "sample"}
    if extra_dir:
        profiles = {}
        try:
            profiles = json.loads((extra_dir / ".profiles.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        for path in sorted(extra_dir.glob("*.txt")):
            meta = profiles.get(path.name, {})
            if meta.get("format_id", "gen9ou") != "gen9ou":
                continue
            teams["user:" + path.stem] = {"text": path.read_text(encoding="utf-8-sig"),
                                         "archetype": meta.get("archetype", "auto"), "source": "user"}
    for prefix, source, candidates in (("seed:", "seed", ROOT / "tools/library_seed.txt"),):
        if not candidates.exists():
            continue
        for block in candidates.read_text(encoding="utf-8").split("=== ")[1:]:
            header, _, text = block.partition(chr(10))
            name, _, archetype = header.partition("|")
            teams[prefix + name.strip()] = {"text": text.strip() + chr(10), "archetype": archetype.strip() or "auto",
                                            "source": source}
    valid = {}
    for name, team in teams.items():
        problems = validator.validate_team(team["text"], "gen9ou")
        if problems:
            print(f"skip {name}: {problems[0]}", flush=True)
        else:
            valid[name] = team
    return valid


def make_player(name: str, team: dict, budget: int, meta) -> StrategicPlayer:
    search = SimulatorSearch(meta=meta, time_budget_ms=budget, workers=1)
    return StrategicPlayer(
        engine=DecisionEngine(meta=meta), search=search, emit=lambda event: None,
        archetype=team["archetype"], account_configuration=AccountConfiguration(name, None),
        battle_format="gen9ou", team=team["text"], server_configuration=LocalhostServerConfiguration,
        max_concurrent_battles=1, log_level=logging.ERROR, start_timer_on_battle_start=False,
        learning_enabled=False)


async def play_pair(index: int, left: str, right: str, teams: dict, games: int, budget: int, meta) -> list[dict]:
    tag = f"{index:04d}{random.randrange(1000):03d}"
    a = make_player(f"arenaa{tag}", teams[left], budget, meta)
    b = make_player(f"arenab{tag}", teams[right], budget, meta)
    results = []
    try:
        began = time.monotonic()
        await asyncio.wait_for(a.battle_against(b, n_battles=games), timeout=games * 1800)
        # Finished rooms are released by the bot, so count from the totals.
        results += [{"a": left, "b": right, "winner": left}] * a.n_won_battles
        results += [{"a": left, "b": right, "winner": right}] * b.n_won_battles
        results += [{"a": left, "b": right, "winner": None}] * a.n_tied_battles
        print(f"{left} vs {right}: {a.n_won_battles}-{b.n_won_battles} ({time.monotonic() - began:.0f}s)", flush=True)
    except Exception as exc:
        print(f"{left} vs {right}: error {type(exc).__name__}: {exc}", flush=True)
    finally:
        for player in (a, b):
            player.search.close()
            try:
                await player.ps_client.stop_listening()
            except Exception:
                pass
    return results


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=2)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--teams", type=Path, default=ROOT / "teams")
    parser.add_argument("--only", nargs="*", help="Play only pairs involving these teams")
    parser.add_argument("--among", action="store_true", help="With --only: only pairs where both teams are listed")
    parser.add_argument("--gauntlet", action="store_true", help="Candidates and user teams only play the sample teams")
    parser.add_argument("--out", type=Path, default=ROOT / "arena.json")
    args = parser.parse_args()
    meta = MetaStore(ROOT / "cache").load("gen9ou")
    validator = SimulatorSearch(workers=1)
    teams = load_teams(args.teams, validator)
    validator.close()
    print(f"{len(teams)} valid teams", flush=True)
    pairs = [(x, y) for x, y in itertools.combinations(sorted(teams), 2)
             if not args.only or x in args.only or y in args.only]
    if args.among and args.only:
        pairs = [(x, y) for x, y in pairs if x in args.only and y in args.only]
    if args.gauntlet:
        pairs = [(x, y) for x, y in pairs if (teams[x]["source"] == "sample") != (teams[y]["source"] == "sample")]
    random.shuffle(pairs)
    existing = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else []
    gate = asyncio.Semaphore(args.parallel)

    async def run(index, pair):
        async with gate:
            rows = await play_pair(index, *pair, teams, args.games, args.budget, meta)
            existing.extend(rows)
            args.out.write_text(json.dumps(existing, indent=1), encoding="utf-8")

    await asyncio.gather(*(run(i, pair) for i, pair in enumerate(pairs)))
    table = {}
    for row in existing:
        for side in ("a", "b"):
            entry = table.setdefault(row[side], [0, 0])
            entry[1] += 1
            entry[0] += row["winner"] == row[side]
    for name, (wins, total) in sorted(table.items(), key=lambda item: -item[1][0] / max(1, item[1][1])):
        print(f"{wins / max(1, total):6.1%}  {wins:3d}/{total:<3d}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
