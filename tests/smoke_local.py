"""Optional complete local battles; requires Showdown on localhost:8000.

Run from the project: python tests/smoke_local.py --games 6
Never connects to or searches the public ladder. Test history uses a temp folder.
"""
import argparse
import asyncio
import logging
import json
from pathlib import Path
from contextlib import nullcontext
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from poke_env.player import SimpleHeuristicsPlayer
from poke_env.ps_client import AccountConfiguration
from poke_env.ps_client.server_configuration import LocalhostServerConfiguration
from unaware.runner import StrategicPlayer
from unaware.engine import DecisionEngine
from unaware.learning import ExperienceStore, team_fingerprint
from unaware.meta import MetaStore
from unaware.search import SimulatorSearch

async def run(args):
    root = Path(__file__).resolve().parents[1]
    team = args.team.read_text(encoding="utf-8-sig") if args.team else None
    if args.format != "gen9randombattle" and not team:
        raise ValueError("Pass --team for a format with built teams.")
    if args.storage:
        args.storage.mkdir(parents=True, exist_ok=True)
    with (nullcontext(str(args.storage)) if args.storage else tempfile.TemporaryDirectory(prefix="showdown-pilot-smoke-")) as folder:
        base = Path(folder)
        events = []
        def emit(event):
            events.append(event)
            if event["type"] in {"status", "log"}:
                print(event["message"], flush=True)
            elif event["type"] == "decision":
                print(f"Turno {event.get('turn')}: {event.get('evaluation_mode')} ({event.get('search', {}).get('elapsed_ms', 0)} ms)", flush=True)
        loop = asyncio.get_running_loop()
        nonce = str(time.time_ns())[-6:]
        search = SimulatorSearch(meta=MetaStore(root/"cache").load(args.format), time_budget_ms=args.budget)
        search.ready()
        player = StrategicPlayer(
            engine=DecisionEngine(meta=MetaStore(root/"cache").load(args.format)), emit=emit,
            search=search,
            archetype=args.archetype,
            experience=ExperienceStore(base), team_id=team_fingerprint(team or "", args.format),
            base_dir=base, account_configuration=AccountConfiguration("Pilot"+nonce, None),
            server_configuration=LocalhostServerConfiguration,
            battle_format=args.format, team=team, loop=loop, log_level=logging.WARNING, start_listening=False,
        )
        foe = SimpleHeuristicsPlayer(account_configuration=AccountConfiguration("Peer"+nonce, None),
            server_configuration=LocalhostServerConfiguration,
            battle_format=args.format, team=team, loop=loop, log_level=logging.WARNING, start_listening=False)
        listeners = [asyncio.create_task(p.ps_client.listen()) for p in (player, foe)]
        try:
            await asyncio.wait_for(player.battle_against(foe, n_battles=args.games), 1200)
            if player._report_jobs:
                await asyncio.wait_for(asyncio.gather(*tuple(player._report_jobs)), 10)
            history = ExperienceStore(base).history()
            assert player.n_finished_battles == args.games
            assert player.fallback_count == 0, f"fallbacks={player.fallback_count}"
            assert len(history) == args.games, "Not every battle was saved."
            for record in history:
                assert Path(record["analysis_path"]).is_file()
                assert Path(record["replay_path"]).is_file()
            decisions = sum(e["type"] == "decision" for e in events)
            assert all(e.get("evaluation_mode") == "probabilistic+simulator" for e in events if e["type"] == "decision")
            loss_reports = sum(e["type"] == "learning" for e in events)
            assert loss_reports == player.n_lost_battles
            print(f"PASS: {args.games} battles ({player.n_won_battles}W/{player.n_lost_battles}L), {decisions} simulated decisions, {loss_reports} loss reports, 0 fallbacks.")
            search_events = [event.get("search", {}) for event in events if event["type"] == "decision"]
            metrics = {"games": args.games, "format": args.format, "wins": player.n_won_battles,
                       "archetype": player.strategy_profile.get("archetype"),
                       "losses": player.n_lost_battles, "decisions": decisions,
                       "fallbacks": player.fallback_count, "loss_reports": loss_reports,
                       "depth_two_decisions": sum(event.get("depth") == 2 for event in search_events),
                       "average_ms": round(sum(event.get("elapsed_ms", 0) for event in search_events) / max(1, decisions)),
                       "max_ms": max((event.get("elapsed_ms", 0) for event in search_events), default=0)}
            print(json.dumps(metrics), flush=True)
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
        finally:
            if args.storage:
                (base / "test-events.json").write_text(json.dumps(events, ensure_ascii=False, default=str), encoding="utf-8")
            search.close()
            for p in (player, foe):
                if hasattr(p.ps_client, "websocket"):
                    await p.ps_client.websocket.close()
            for listener in listeners:
                listener.cancel()
            await asyncio.gather(*listeners, return_exceptions=True)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--games", type=int, default=6)
    parser.add_argument("--format", default="gen9randombattle")
    parser.add_argument("--archetype", default="auto")
    parser.add_argument("--team", type=Path)
    parser.add_argument("--budget", type=int, default=1500)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--storage", type=Path, help="Optional separate folder to inspect the test replays")
    asyncio.run(run(parser.parse_args()))
