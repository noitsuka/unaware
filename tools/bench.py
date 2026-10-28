"""Brain benchmark on the local server (node pokemon-showdown start --no-security 8000).

    .venv/Scripts/python tools/bench.py headtohead --games 40 --parallel 4
    .venv/Scripts/python tools/bench.py baseline --games 20 --brain v2
    .venv/Scripts/python tools/bench.py headtohead --a foulplay@8x3000 --b foulplay@4x2000 --parallel 1

headtohead: brain v2 vs legacy, both sides with the same team (rotating over
the selected teams), sides alternating, so only the decision logic differs.
baseline: our bot vs poke-env's SimpleHeuristicsPlayer with sample OU teams.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from poke_env.player import SimpleHeuristicsPlayer  # noqa: E402
from poke_env.ps_client import AccountConfiguration  # noqa: E402
from poke_env.ps_client.server_configuration import LocalhostServerConfiguration  # noqa: E402

from arena import load_teams, make_player  # noqa: E402
from unaware.meta import MetaStore  # noqa: E402
from unaware.search import SimulatorSearch  # noqa: E402

DEFAULT_TEAMS = ["user:Bellibolt Rain 2062 by rza", "user:Ebelt Zama Offense by MAVE 61OXI50o",
                 "user:Starter Balance", "user:Starter HO"]


def bot(name: str, team: dict, budget: int, meta, brain: str):
    player = make_player(name, team, budget, meta)
    # "foulplay@8x3000": Foul Play with 8 sampled states of 3000 ms each.
    brain, _, size = brain.partition("@")
    # "hybrid@8x3000": Foul Play decides, the network breaks its near-ties.
    if brain == "hybrid":
        player.foulplay_engine, brain = brain, "foulplay"
    # ".../noloop": without the battle memory that leaves loops (A/B only).
    # ".../noloop", ".../noend": without the loop memory / the endgame search time (A/B only).
    while size.endswith(("/noloop", "/noend", "/noguard")):
        if size.endswith("/noguard"):
            player.foulplay_guards, size = False, size.removesuffix("/noguard")
        elif size.endswith("/noloop"):
            player.foulplay_loops, size = False, size.removesuffix("/noloop")
        else:
            player.foulplay_endgame, size = False, size.removesuffix("/noend")
    # ".../w32": Foul Play samples 32 opponent worlds per decision in the same
    # turn time; "/w0" keeps Foul Play's own count (8, 16 early).
    worlds = re.search(r"/w(\d+)", size)
    if worlds:
        player.foulplay_worlds, size = int(worlds.group(1)), size.replace(worlds.group(0), "")
    if size:
        samples, _, ms = size.partition("x")
        player.foulplay_parallelism, player.foulplay_search_ms = int(samples), int(ms)
    # "foulplay": Foul Play decides, v2 search is its fallback.
    player.brain_mode = "foulplay" if brain == "foulplay" else "own"
    brain = "v2" if brain == "foulplay" else brain
    player.search.brain = brain
    for item in getattr(player, "_pool", None) or []:
        item.brain = brain
    player.search_overrides = {"brain": brain}
    player.modes = {}

    def emit(record):
        if record.get("type") == "decision":
            mode = record.get("evaluation_mode", "?")
            player.modes[mode] = player.modes.get(mode, 0) + 1
    player.emit = emit
    return player


async def close(*players):
    for player in players:
        search = getattr(player, "search", None)
        if search is not None:
            search.close()
        try:
            await player.ps_client.stop_listening()
        except Exception:
            pass


async def head_to_head(index, team_name, teams, budget, meta, v2_first: bool, brains=("v2", "legacy")):
    tag = f"{index:04d}{random.randrange(1000):03d}"
    v2 = bot(f"benchv{tag}", teams[team_name], budget, meta, brains[0])
    old = bot(f"benchl{tag}", teams[team_name], budget, meta, brains[1])
    first, second = (v2, old) if v2_first else (old, v2)
    try:
        await asyncio.wait_for(first.battle_against(second, n_battles=1), timeout=1800)
        return {"team": team_name, "v2": v2.n_won_battles, "legacy": old.n_won_battles,
                "modes_v2": v2.modes, "modes_legacy": old.modes, "turns": max((b.turn for b in v2.battles.values()), default=0)}
    except Exception as exc:
        return {"team": team_name, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        await close(v2, old)


async def baseline(index, team_name, foe_name, teams, budget, meta, brain):
    tag = f"{index:04d}{random.randrange(1000):03d}"
    ours = bot(f"benchb{tag}", teams[team_name], budget, meta, brain)
    foe = SimpleHeuristicsPlayer(account_configuration=AccountConfiguration(f"benchh{tag}", None),
                                 battle_format="gen9ou", team=teams[foe_name]["text"],
                                 server_configuration=LocalhostServerConfiguration, log_level=logging.ERROR)
    try:
        await asyncio.wait_for(ours.battle_against(foe, n_battles=1), timeout=1800)
        return {"team": team_name, "foe": foe_name, "won": ours.n_won_battles, "lost": foe.n_won_battles, "modes": ours.modes}
    except Exception as exc:
        return {"team": team_name, "foe": foe_name, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        await close(ours, foe)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["headtohead", "baseline"])
    parser.add_argument("--games", type=int, default=20)
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--brain", default="v2")
    parser.add_argument("--a", default="v2", help="headtohead: first brain (reported as v2)")
    parser.add_argument("--b", default="legacy", help="headtohead: second brain (reported as legacy)")
    parser.add_argument("--teams", nargs="*", default=DEFAULT_TEAMS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    meta = MetaStore(ROOT / "cache").load("gen9ou")
    validator = SimulatorSearch(workers=1)
    teams = load_teams(ROOT / "teams", validator)
    validator.close()
    ours = [name for name in args.teams if name in teams]
    samples = [name for name, team in teams.items() if team["source"] == "sample"]
    gate = asyncio.Semaphore(args.parallel)
    results = []
    began = time.monotonic()

    async def run(index):
        async with gate:
            team = ours[index % len(ours)]
            if args.mode == "headtohead":
                row = await head_to_head(index, team, teams, args.budget, meta, v2_first=index % 2 == 0, brains=(args.a, args.b))
            else:
                row = await baseline(index, team, random.choice(samples), teams, args.budget, meta, args.brain)
            results.append(row)
            print(json.dumps(row), flush=True)

    await asyncio.gather(*(run(i) for i in range(args.games)))
    if args.mode == "headtohead":
        wins = sum(r.get("v2", 0) for r in results)
        losses = sum(r.get("legacy", 0) for r in results)
        print(f"{args.a} {wins} x {losses} {args.b} ({wins / max(1, wins + losses):.0%}) in {time.monotonic() - began:.0f}s", flush=True)
    else:
        wins = sum(r.get("won", 0) for r in results)
        losses = sum(r.get("lost", 0) for r in results)
        print(f"{args.brain} {wins} x {losses} heuristic ({wins / max(1, wins + losses):.0%}) in {time.monotonic() - began:.0f}s", flush=True)
    if args.out:
        args.out.write_text(json.dumps(results), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
