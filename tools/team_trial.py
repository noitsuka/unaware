"""Which team does the bot win most with? Local server, bot (brain v2) on both sides.

    .venv/Scripts/python tools/team_trial.py --games 6 --parallel 3 --out trial.json
    .venv/Scripts/python tools/team_trial.py --only "Band Zamazenta Balance" ... --games 20

Candidates: the 21 official Smogon SV OU samples (post Tera Blast ban, tools/samples2026)
plus the user's teams. Each game: candidate vs an opponent drawn from the samples
(never itself). Results append to --out so stages can be combined.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from bench import bot, close  # noqa: E402
from unaware.meta import MetaStore  # noqa: E402

USER = ["Bellibolt Rain 2062 by rza", "Ebelt Zama Offense by MAVE 61OXI50o"]


def load() -> dict:
    teams = {}
    folder = ROOT / "tools" / "samples2026"
    styles = dict(line.split("|") for line in (folder / ".index").read_text(encoding="utf-8").split(chr(10)) if "|" in line)
    for path in sorted(folder.glob("*.txt")):
        teams[path.stem] = {"text": path.read_text(encoding="utf-8"), "archetype": styles.get(path.stem, "auto"), "source": "sample"}
    for name in USER:
        path = ROOT / "teams" / f"{name}.txt"
        if path.exists():
            teams[name] = {"text": path.read_text(encoding="utf-8-sig"), "archetype": "auto", "source": "user"}
    return teams


async def game(index, candidate, opponent, teams, budget, meta, brain="v2", foe_brain="v2"):
    tag = f"{index:04d}{random.randrange(1000):03d}"
    ours = bot(f"trialc{tag}", teams[candidate], budget, meta, brain)
    foe = bot(f"trialo{tag}", teams[opponent], budget, meta, foe_brain)
    first, second = (ours, foe) if index % 2 == 0 else (foe, ours)
    try:
        await asyncio.wait_for(first.battle_against(second, n_battles=1), timeout=1800)
        return {"team": candidate, "foe": opponent, "won": ours.n_won_battles, "lost": foe.n_won_battles}
    except Exception as exc:
        return {"team": candidate, "foe": opponent, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        await close(ours, foe)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--games", type=int, default=6, help="games per candidate")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--brain", default="v2", help="pilot of the candidate team (v2/foulplay/legacy)")
    parser.add_argument("--foe-brain", default="v2")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--extra", nargs="*", default=[], help="more candidate team files (name = file stem)")
    parser.add_argument("--foes", nargs="*", help="paired trial: every candidate plays --games games against each of these")
    args = parser.parse_args()
    meta = MetaStore(ROOT / "cache").load("gen9ou")
    teams = load()
    for extra in args.extra:
        path = Path(extra)
        teams[path.stem] = {"text": path.read_text(encoding="utf-8-sig"), "archetype": "auto", "source": "user"}
    samples = [name for name, team in teams.items() if team["source"] == "sample"]
    candidates = [name for name in teams if not args.only or name in args.only]
    if args.foes:
        # Paired: same opponents and sides for every candidate, so only the team differs.
        # Job index decides the side (index % 2): each candidate plays its games
        # against a foe in consecutive slots, so an even --games gives both sides.
        jobs = [(c, foe) for foe in args.foes for c in candidates for _ in range(args.games)]
    else:
        jobs = [(c, random.choice([s for s in samples if s != c])) for c in candidates for _ in range(args.games)]
        random.shuffle(jobs)
    results = json.loads(args.out.read_text(encoding="utf-8")) if args.out.exists() else []
    gate, lock = asyncio.Semaphore(args.parallel), asyncio.Lock()
    began = time.monotonic()

    async def run(index, candidate, opponent):
        async with gate:
            row = await game(index, candidate, opponent, teams, args.budget, meta, args.brain, args.foe_brain)
            async with lock:
                results.append(row)
                args.out.write_text(json.dumps(results), encoding="utf-8")
            print(json.dumps(row), flush=True)

    await asyncio.gather(*(run(i, c, o) for i, (c, o) in enumerate(jobs)))
    table = {}
    for row in results:
        entry = table.setdefault(row["team"], [0, 0])
        entry[0] += row.get("won", 0)
        entry[1] += row.get("lost", 0)
    for name, (won, lost) in sorted(table.items(), key=lambda kv: -kv[1][0] / max(1, sum(kv[1]))):
        print(f"{won / max(1, won + lost):6.0%}  {won:3d}-{lost:<3d} {name}", flush=True)
    print(f"{time.monotonic() - began:.0f}s", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
