"""GUI by default; reproducible CLI for headless sessions and team validation."""
import argparse
import getpass
import json
import os
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description="Unaware: autonomous Pokémon Showdown player (Gen 9 singles)")
    parser.add_argument("--base-dir", type=Path, default=Path.cwd(), help="Folder with teams, cache, logs and replays")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="Open the launcher (default)")
    preview = sub.add_parser("preview", help="Serve the interface locally for development")
    preview.add_argument("--port", type=int, default=8765)
    validate = sub.add_parser("validate", help="Validate an exported team")
    validate.add_argument("team_file", type=Path)
    validate.add_argument("--format", default="gen9ou")
    meta = sub.add_parser("update-meta", help="Download the latest published month for the format")
    meta.add_argument("--format", default="gen9ou")
    meta.add_argument("--rating", type=int, default=1695)
    run = sub.add_parser("run", help="Play without the interface")
    run.add_argument("--username", required=True)
    run.add_argument("--password-prompt", action="store_true", help="Ask for the password without echoing it; or set SHOWDOWN_PASSWORD")
    run.add_argument("--format", default="gen9ou")
    run.add_argument("--archetype", choices=["auto", "hyper_offense", "bulky_offense", "balance", "semi_stall", "stall", "weather"], default="auto")
    run.add_argument("--team", type=Path)
    run.add_argument("--mode", choices=["ladder", "challenge", "accept"], default="ladder")
    run.add_argument("--opponent", default="")
    run.add_argument("--battles", type=int, default=1)
    run.add_argument("--server", choices=["showdown", "local", "custom"], default="showdown")
    run.add_argument("--websocket-url", default="")
    run.add_argument("--authentication-url", default="")
    run.add_argument("--risk", type=float, default=0.35, help="Caution with bad scenarios, between 0 and 1")
    run.add_argument("--no-tera", action="store_true")
    run.add_argument("--offline-meta", action="store_true", help="Use the cache only, without updating the stats")
    run.add_argument("--no-learning", action="store_true", help="Analyse and record battles without adjusting preferences")
    args = parser.parse_args()
    if args.command in {None, "gui"}:
        from .gui import launch, startup_error
        try:
            launch(args.base_dir)
        except Exception as exc:
            startup_error(args.base_dir, exc)
            raise SystemExit(1) from exc
    elif args.command == "preview":
        from .backend import serve
        service, server = serve(args.base_dir, args.port)
        print(server.origin, flush=True)
        try:
            while True:
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            service.close()
            server.shutdown()
            server.server_close()
    elif args.command == "validate":
        from .teams import validate_team
        from .search import SimulatorSearch
        text = args.team_file.read_text(encoding="utf-8-sig")
        errors = validate_team(text, args.format)
        if not errors:
            with SimulatorSearch() as simulator:
                errors = simulator.validate_team(text, args.format)
        print("\n".join(errors) if errors else "Team is valid for this format (official validator).")
        raise SystemExit(bool(errors))
    elif args.command == "update-meta":
        from .meta import MetaStore
        snapshot = MetaStore(args.base_dir / "cache").update(args.format, args.rating)
        print(f"Smogon {snapshot.month} • cutoff {snapshot.rating}\n{snapshot.source_url}")
        for warning in snapshot.warnings:
            print(warning)
    else:
        from .runner import BotConfig, BotRunner
        password = getpass.getpass("Showdown password: ") if args.password_prompt else os.environ.get("SHOWDOWN_PASSWORD", "")
        config = BotConfig(
            username=args.username, password=password, format_id=args.format,
            archetype=args.archetype,
            team=args.team.read_text(encoding="utf-8-sig") if args.team else "",
            mode=args.mode, opponent=args.opponent, battles=args.battles,
            server=args.server, websocket_url=args.websocket_url,
            authentication_url=args.authentication_url, risk=args.risk,
            allow_tera=not args.no_tera, auto_update_meta=not args.offline_meta,
            learning_enabled=not args.no_learning,
        )
        failures = []
        def event(data):
            if data["type"] == "error":
                failures.append(data)
            print(json.dumps(data, ensure_ascii=False), flush=True)
        runner = BotRunner(config, event, args.base_dir)
        try:
            runner.start()
            while runner.is_running:
                try:
                    time.sleep(0.2)
                except KeyboardInterrupt:
                    runner.stop()
                    print("Cancelling pending actions and disconnecting.")
        except ValueError as exc:
            parser.error(str(exc))
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
