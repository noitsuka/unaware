"""Local React API and session state. Battle evaluation never runs on the UI thread."""
from __future__ import annotations

from dataclasses import asdict, fields, replace
from datetime import datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import math
from pathlib import Path
import queue
import re
import secrets
import shutil
import threading
from urllib.parse import parse_qs, unquote, urlparse

from .learning import ExperienceStore
from .meta import MetaStore
from .runner import BotConfig, BotRunner
from .teams import FORMAT_CHOICES, parse_team, validate_team
from .strategy import LABELS, analyze_team
from .session import SessionStore
from . import __version__

# Whole history kept for the per-team totals; the UI list gets the latest 100.
HISTORY_LIMIT = 5000


class AppService:
    def __init__(self, base_dir: Path, runner_factory=BotRunner):
        self.base_dir = Path(base_dir).resolve()
        self.team_dir = self.base_dir / "teams"
        self.team_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.team_metadata = json.loads((self.team_dir / ".profiles.json").read_text(encoding="utf-8"))
            if not isinstance(self.team_metadata, dict):
                self.team_metadata = {}
        except (OSError, ValueError):
            self.team_metadata = {}
        self.experience = ExperienceStore(self.base_dir)
        self.runner_factory = runner_factory
        self.runner = None
        self.sessions = SessionStore(self.base_dir)
        self._restart_timer = None
        self.lock = threading.RLock()
        self.subscribers = set()
        self.closed = threading.Event()
        self.config = BotConfig()
        self.selected_team = ""
        self.status, self.status_message, self.error = "Idle", "", ""
        self.running = False
        self.recover_pending = False
        self.on_finished = None
        self.decision = None
        self.metrics = {"cpu": None, "latency_ms": None, "last_sync": None, "response_ms": None}
        self.history = self.experience.history(HISTORY_LIMIT)
        self.live: dict[str, dict] = {}
        self.search_health = {"full": 0, "total": 0}
        try:
            saved = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8-sig"))
            allowed = {field.name for field in fields(BotConfig)} - {"password"}
            self.config = BotConfig(**{key: value for key, value in saved.items() if key in allowed})
            if "autoplay" not in saved:
                self.config.autoplay = bool(saved.get("battles", 1) > 1)
            self.config.battles = 1
            self.selected_team = str(saved.get("selected_team", ""))
            if self.config.format_id not in FORMAT_CHOICES:
                self.config.format_id = "gen9ou"
        except (OSError, ValueError, TypeError):
            pass
        if not self.selected_team and list(self.team_dir.glob("*.txt")):
            self.selected_team = sorted(self.team_dir.glob("*.txt"))[0].name
        cached = MetaStore(self.base_dir / "cache").load(self.config.format_id)
        if cached:
            self.metrics["last_sync"] = cached.downloaded_at or None

    def _team_path(self, name):
        name = str(name or "").strip()
        if not name.endswith(".txt"):
            name += ".txt"
        if not name or name == ".txt" or re.search(r'[<>:"/\\|?*\x00-\x1f]', name):
            raise ValueError("Invalid team name.")
        if name.rstrip(". ") != name or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
            raise ValueError("That file name is reserved by Windows.")
        path = (self.team_dir / name).resolve()
        if path.parent != self.team_dir.resolve():
            raise ValueError("The file must be inside the teams folder.")
        return path

    def teams(self):
        signature = tuple((path.name, path.stat().st_mtime_ns) for path in sorted(self.team_dir.glob("*.txt")))
        signature += (json.dumps(self.team_metadata, sort_keys=True),)
        cached = getattr(self, "_teams_cache", None)
        if cached and cached[0] == signature:
            return cached[1]
        result = self._load_teams()
        self._teams_cache = (signature, result)
        return result

    def _team_records(self) -> dict:
        from .learning import team_fingerprint
        by_team = {}
        for record in self.history:
            if (record.get("format_id") != self.config.format_id
                    or str(record.get("username", "")).casefold() != self.config.username.casefold()):
                continue
            entry = by_team.setdefault(record.get("team_id"), [0, 0])
            entry[0] += record.get("result") == "win"
            entry[1] += record.get("result") == "loss"
        result = {}
        for team in self.teams():
            fingerprint = team_fingerprint(team.get("text", ""), team.get("format_id", self.config.format_id))
            wins, losses = by_team.get(fingerprint, (0, 0))
            if wins or losses:
                result[team["name"]] = {"wins": wins, "losses": losses}
        return result

    def _team_elo(self) -> dict:
        """Elo won or lost with the selected team in this format: the sum of every
        rated game's change (the launcher's +/- next to the rating)."""
        from .learning import team_fingerprint
        try:
            # config.team is only filled when a session starts; read the selected file.
            text = self._team_path(self.selected_team).read_text(encoding="utf-8-sig") if self.selected_team else self.config.team
            fingerprint = team_fingerprint(text, self.config.format_id)
        except Exception:
            return {"delta": None, "games": 0}
        # Parallel battles each store the rating from when they started, so
        # after - before double-counts overlapping games. Measure each game from
        # the rating left by the previous game that ended on this account/format.
        delta = games = 0
        previous = None
        for record in sorted(self.history, key=lambda item: str(item.get("ended_at") or "")):
            before, after = record.get("rating_before"), record.get("rating_after")
            if (record.get("format_id") != self.config.format_id
                    or str(record.get("username", "")).casefold() != self.config.username.casefold()
                    or not isinstance(after, (int, float))):
                continue
            start = previous if previous is not None else before
            previous = after
            if record.get("team_id") == fingerprint and isinstance(start, (int, float)):
                delta += after - start
                games += 1
        return {"delta": round(delta) if games else None, "games": games}

    # 3) Library: researched and recommended teams, importable in one click.
    def library(self) -> list[dict]:
        """Teams > Library: assets/library.txt (tools/build_library.py), parsed once per file change."""
        from .strategy import SETUP
        path = self.base_dir / "assets" / "library.txt"
        try:
            stamp = path.stat().st_mtime
        except OSError:
            return []
        cached = getattr(self, "_library_cache", None)
        if cached and cached[0] == stamp:
            return cached[1]
        rows = []
        for block in path.read_text(encoding="utf-8").split("=== ")[1:]:
            header, _, text = block.partition("\n")
            parts = [part.strip() for part in header.split("|")]
            name, archetype = parts[0], parts[1] if len(parts) > 1 else "auto"
            format_id = parts[2] if len(parts) > 2 else "gen9ou"
            source = parts[3] if len(parts) > 3 else "sample"
            try:
                members = parse_team(text)
            except ValueError:
                continue
            setup = sum(any("".join(c for c in move.lower() if c.isalnum()) in SETUP for move in m.moves) for m in members)
            rows.append({"name": name, "source": source, "format_id": format_id, "text": text.strip() + "\n",
                         "archetype": archetype, "species": [m.species for m in members], "setup_users": setup})
        self._library_cache = (stamp, rows)
        return rows

    def import_library_team(self, supplied):
        name = str(supplied.get("name", ""))
        row = next((item for item in self.library() if item["name"] == name), None)
        if row is None:
            raise ValueError("Team not found in the library.")
        target = "".join(c for c in name if c.isalnum() or c in " +-_").strip()[:40] or "Team"
        archetype = row["archetype"] if row["archetype"] in LABELS else "auto"
        self.save_team({"name": target, "text": row["text"], "format_id": row["format_id"], "archetype": archetype})

    def reset_learning(self, _=None):
        with self.lock:
            if self.running:
                raise ValueError("Stop the bot before clearing what it learned.")
            self.experience.reset_learning()
        self.publish()

    def _load_teams(self):
        from poke_env.data import GenData
        pokedex = GenData.from_gen(9).pokedex
        result = []
        for path in sorted(self.team_dir.glob("*.txt")):
            try:
                self._team_path(path.name)
                text = path.read_text(encoding="utf-8-sig")
                parsed = parse_team(text)
                members = [asdict(member) for member in parsed]
                for member in members:
                    key = "".join(c for c in member["species"].lower() if c.isalnum())
                    entry = pokedex.get(key, {})
                    member["types"] = entry.get("types", [])
                    # Showdown sprite id: base species + "-" + forme (slowking-galar).
                    base = "".join(c for c in str(entry.get("baseSpecies", "")).lower() if c.isalnum())
                    forme = "".join(c for c in str(entry.get("forme", "")).lower() if c.isalnum())
                    member["sprite"] = f"{base}-{forme}" if base and forme else key
                metadata = self.team_metadata.get(path.name, {})
                if not isinstance(metadata, dict):
                    metadata = {}
                archetype = metadata.get("archetype", "auto")
                if not isinstance(archetype, str) or archetype not in {"auto", *LABELS}:
                    archetype = "auto"
                tier = metadata.get("format_id", self.config.format_id)
                if not isinstance(tier, str) or tier not in FORMAT_CHOICES:
                    tier = "gen9ou"
                result.append({"name": path.name, "label": path.stem, "text": text, "members": members,
                               "format_id": tier, "archetype": archetype, "profile": analyze_team(parsed, archetype)})
            except (ValueError, OSError):
                # Still expose the file for repair in the export editor.
                result.append({"name": path.name, "label": path.stem, "text": "", "members": [], "error": "Invalid export or file not available."})
        return result

    def _namespace_matches(self, namespace):
        if self.config.server == "showdown":
            hostname = urlparse("//" + str(namespace or "")).hostname or ""
            return namespace == "showdown" or hostname.endswith(".psim.us")
        if self.config.server == "local":
            return namespace in {"localhost:8000", "127.0.0.1:8000"}
        return namespace == urlparse(self.config.websocket_url).netloc

    def state(self):
        with self.lock:
            ratings = {}
            for record in self.history:
                tier = record.get("format_id")
                if tier not in ratings and str(record.get("username", "")).casefold() == self.config.username.casefold() and self._namespace_matches(record.get("server_namespace")) and record.get("rating_after") is not None:
                    ratings[tier] = record["rating_after"]
            config = self.config.public_dict()
            config.pop("team", None)
            namespace = ("showdown" if self.config.server == "showdown" else "localhost:8000"
                         if self.config.server == "local" else urlparse(self.config.websocket_url).netloc)
            key = (self.config.format_id, self.config.username, namespace, len(self.history),
                   self.history[0].get("battle_id") if self.history else "")
            cached = getattr(self, "_performance", None)
            if cached and cached[0] == key:
                performance = cached[1]
            else:
                performance = (self.experience.statistics(self.config.format_id, self.config.username, namespace)
                               if self.config.username.strip() else
                               {"wins": 0, "losses": 0, "ties": 0, "total": 0, "winrate": 0.})
                self._performance = (key, performance)
            return {"config": config, "teams": self.teams(), "selected_team": self.selected_team,
                    "format_choices": FORMAT_CHOICES, "history": self.history[:100], "ratings": ratings,
                    "performance": performance,
                    "status": self.status, "status_message": self.status_message, "error": self.error,
                    "running": self.running, "metrics": dict(self.metrics), "decision": self.decision,
                    "live": sorted(self.live.values(), key=lambda row: row.get("started_at", 0)),
                    "account_saved": self.sessions.has_account(self.config.username),
                    "search_health": dict(self.search_health),
                    "team_records": self._team_records(), "team_elo": self._team_elo(), "version": __version__}

    def publish(self):
        message = {"type": "state", "state": self.state()}
        with self.lock:
            for mailbox in list(self.subscribers):
                try:
                    mailbox.put_nowait(message)
                except queue.Full:
                    try:
                        mailbox.get_nowait()
                        mailbox.put_nowait(message)
                    except queue.Empty:
                        pass

    def save_preferences(self):
        data = self.config.public_dict()
        data.pop("team", None)
        data["selected_team"] = self.selected_team
        temporary = self.base_dir / "config.json.tmp"
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.base_dir / "config.json")

    LIVE_SETTINGS = {"cpu_level", "avatar"}

    def update_config(self, supplied):
        with self.lock:
            supplied = supplied.get("config", supplied)
            if self.running and isinstance(supplied, dict):
                # Viewer/CPU preferences apply while playing; account, tier and
                # team changes still wait for the session to end.
                changed = {key for key, value in supplied.items()
                           if key in {f.name for f in fields(BotConfig)} and key != "password"
                           and getattr(self.config, key) != value}
                if changed - self.LIVE_SETTINGS or supplied.get("selected_team", self.selected_team) not in {"", self.selected_team} or supplied.get("password"):
                    raise ValueError("Finish the session before changing account, format or team.")
            if not isinstance(supplied, dict):
                raise ValueError("Invalid settings.")
            secret = supplied.get("password")
            supplied = {key: value for key, value in supplied.items() if key != "password"}
            if isinstance(secret, str) and secret:
                username = str(supplied.get("username", self.config.username))
                self.sessions.save_account(username, secret)
            allowed = {field.name for field in fields(BotConfig)}
            changes = {key: value for key, value in supplied.items() if key in allowed}
            for key, value in changes.items():
                expected = type(getattr(BotConfig(), key))
                if expected in {int, float}:
                    valid = type(value) in {int, float} and math.isfinite(value) and (expected is float or type(value) is int)
                else:
                    valid = type(value) is expected
                if not valid:
                    raise ValueError(f"Invalid value for {key}.")
            candidate = replace(self.config, **changes)
            if candidate.format_id not in FORMAT_CHOICES:
                raise ValueError("Invalid format.")
            if candidate.archetype not in {"auto", *LABELS}:
                raise ValueError("Invalid strategy.")
            if candidate.server not in {"showdown", "local", "custom"} or candidate.mode not in {"ladder", "challenge", "accept"}:
                raise ValueError("Invalid mode or server.")
            if not 1 <= candidate.battles <= 100 or not 0 <= candidate.risk <= 1 or not 10 <= candidate.matchmaking_timeout <= 3600:
                raise ValueError("Invalid number of battles, risk or matchmaking timeout.")
            if not 1 <= candidate.concurrent_battles <= 5:
                raise ValueError("Choose 1 to 5 concurrent battles.")
            if candidate.cpu_level not in {"low", "medium", "high"}:
                raise ValueError("Invalid CPU usage level.")
            if candidate.engine not in {"foulplay", "hybrid"}:
                raise ValueError("Invalid decision engine.")
            candidate.avatar = candidate.avatar.strip().lower()
            if candidate.avatar and not re.fullmatch(r"[a-z0-9-]{1,40}", candidate.avatar):
                raise ValueError("Invalid avatar name: use the /avatar name (e.g. lucas).")
            selected = self.selected_team
            if "selected_team" in supplied and supplied["selected_team"]:
                path = self._team_path(supplied["selected_team"])
                if not path.is_file():
                    raise ValueError("The selected team does not exist.")
                selected = path.name
            avatar_changed = candidate.avatar != self.config.avatar
            self.config, self.selected_team = candidate, selected
            self.save_preferences()
            if avatar_changed and self.runner and self.runner.is_running:
                self.runner.set_avatar(candidate.avatar)
        self.publish()

    def start(self, supplied):
        with self.lock:
            if self.running or (self.runner and self.runner.is_running):
                raise ValueError("A session is already running.")
            self.update_config(supplied)
            # Desktop parameters are managed internally. Old saved sliders or
            # switches must not silently disable research/learning after the UI
            # was simplified. CLI/local test sessions keep their explicit flags.
            self.config.risk = BotConfig().risk
            self.config.matchmaking_timeout = BotConfig().matchmaking_timeout
            self.config.allow_tera = self.config.auto_update_meta = self.config.learning_enabled = True
            self.config.battles = 1
            if self.config.mode != "ladder":
                self.config.concurrent_battles = 1
            self.live.clear()
            self.search_health = {"full": 0, "total": 0}
            if not supplied.get("password") and self.config.username:
                self.config.password = self.sessions.account_password(self.config.username)
            else:
                self.config.password = str(supplied.get("password") or "")
            if self.config.format_id != "gen9randombattle":
                self.config.team = self._team_path(self.selected_team).read_text(encoding="utf-8-sig")
            else:
                self.config.team = ""
            problems = self.config.validate()
            if problems:
                raise ValueError("\n".join(problems))
            self.running, self.error, self.status = True, "", "Starting"
            self.runner = self.runner_factory(replace(self.config), self.handle_event, self.base_dir)
            try:
                self.runner.start()
            except Exception:
                self.running, self.status = False, "Idle"
                raise
        self.publish()

    def stop(self):
        with self.lock:
            self.recover_pending = False
            if self._restart_timer:
                self._restart_timer.cancel()
                self._restart_timer = None
            self.config.autoplay = False
            if self.runner and self.runner.is_running:
                self.runner.stop()
                self.status = "Stopping"
                self.status_message = "Cancelling pending actions and closing the connection."
        self.publish()

    def recover(self):
        with self.lock:
            checkpoint = self.sessions.load()
            if not checkpoint:
                raise ValueError("No saved active battle to recover.")
            if self.config.username and self.config.username.casefold() != checkpoint.get("username", "").casefold():
                raise ValueError("Log in with the account that started the saved battle.")
            if self.runner and self.runner.is_running:
                self.recover_pending = True
                self.runner.stop()
                self.status, self.status_message = "Recovering", "Reconnecting to the saved battle..."
            else:
                self._start_recovery()
        self.publish()

    def _start_recovery(self):
        with self.lock:
            if self.closed.is_set():
                return
            if self.runner and self.runner.is_running:
                self._schedule_restart()
                return
            checkpoint = self.sessions.load()
            if not checkpoint:
                raise ValueError("The saved battle has ended or is no longer available.")
            keys = {"username", "format_id", "archetype", "team", "server", "websocket_url", "authentication_url"}
            config = replace(self.config, **{k: v for k, v in checkpoint.items() if k in keys})
            # The saved login, as a normal start does: the session cookie alone
            # was refused on 16/10 ("Login not confirmed") and the battle was lost.
            if not config.password and config.username:
                config.password = self.sessions.account_password(config.username)
            config.battles = 1
            problems = config.validate()
            if problems:
                raise ValueError("\n".join(problems))
            self.running, self.error, self.status = True, "", "Recovering"
            self.decision = None
            self.runner = self.runner_factory(config, self.handle_event, self.base_dir)
            self.runner.recovery = checkpoint
            try:
                self.runner.start()
            except Exception:
                self.running, self.status = False, "Error"
                raise

    def extra_battle(self, _supplied=None):
        """'+ New battle': one more ladder battle while the current ones keep running."""
        with self.lock:
            if not (self.running and self.runner and self.runner.is_running):
                raise ValueError("Start a session before opening another battle.")
            self.runner.request_extra_battle()
            self.status_message = "Extra battle: searching for an opponent..."
        self.publish()

    def set_autoplay(self, supplied):
        enabled = supplied.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("Autoplay must be on or off.")
        with self.lock:
            if self.running and self.runner and self.runner.is_running:
                self.runner.set_autoplay(enabled)
                if not enabled:
                    self.recover_pending = False
                    self.status = "Stopping"
                    self.status_message = "Autoplay off; finishing the current battle only."
            self.config.autoplay = enabled
            self.config.battles = 1
            self.save_preferences()
        self.publish()

    def save_team(self, supplied):
        with self.lock:
            if self.running:
                raise ValueError("Finish the session before editing the team.")
            path = self._team_path(supplied.get("name"))
            text = str(supplied.get("text", ""))
            tier = supplied.get("format_id", self.config.format_id)
            archetype = supplied.get("archetype", "auto")
            if tier not in FORMAT_CHOICES or tier == "gen9randombattle":
                raise ValueError("Pick a format that uses built teams.")
            if archetype not in {"auto", *LABELS}:
                raise ValueError("Invalid strategy.")
            errors = validate_team(text, tier)
            if errors:
                raise ValueError("\n".join(errors))
            original = supplied.get("original_name")
            original_path = self._team_path(original) if original else None
            if path.exists() and original_path != path:
                raise ValueError("A team with that name already exists; pick another name.")
            if path.exists():
                self._archive_team(path, copy=True)
            temporary = path.with_suffix(".txt.tmp")
            temporary.write_text(text.rstrip() + "\n", encoding="utf-8")
            temporary.replace(path)
            if original_path and original_path != path and original_path.is_file():
                self._archive_team(original_path)
            self.selected_team = path.name
            if original_path and original_path != path:
                self.team_metadata.pop(original_path.name, None)
            self.team_metadata[path.name] = {"format_id": tier, "archetype": archetype}
            self._save_team_metadata()
            self.config.format_id, self.config.archetype = tier, archetype
            self.save_preferences()
        self.publish()

    def _save_team_metadata(self):
        temporary = self.team_dir / ".profiles.json.tmp"
        temporary.write_text(json.dumps(self.team_metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.team_dir / ".profiles.json")

    def analyze_team(self, supplied):
        return analyze_team(parse_team(str(supplied.get("text", ""))), supplied.get("archetype", "auto"))

    def _archive_team(self, path, copy=False):
        archive = self.team_dir / ".trash"
        archive.mkdir(exist_ok=True)
        destination = archive / (datetime.now().strftime("%Y%m%d-%H%M%S-%f-") + path.name)
        # Paths resolved and verified above; deletion is recoverable.
        shutil.copy2(path, destination) if copy else path.replace(destination)

    def delete_team(self, supplied):
        with self.lock:
            if self.running:
                raise ValueError("Finish the session before deleting a team.")
            path = self._team_path(supplied.get("name"))
            if not path.is_file():
                raise ValueError("Team not found.")
            self._archive_team(path)
            self.team_metadata.pop(path.name, None)
            self._save_team_metadata()
            if self.selected_team == path.name:
                remaining = sorted(self.team_dir.glob("*.txt"))
                self.selected_team = remaining[0].name if remaining else ""
            self.save_preferences()
        self.publish()

    def clear_history(self, _):
        with self.lock:
            if self.running or self.recover_pending:
                raise ValueError("Stop the bot before clearing the history.")
            self.experience.clear_history()
            self.history = []
        self.publish()

    def report(self, identifier):
        if "::" in identifier:
            namespace, battle_id = identifier.split("::", 1)
            result = self.experience.get_report(battle_id, namespace)
        else:
            result = self.experience.get_report(identifier)
        if not result:
            raise ValueError("Battle not found.")
        return result

    def open_replay(self, identifier):
        report = self.report(identifier)
        path = Path(report.get("replay_path") or "").resolve()
        if not path.is_file() or not path.is_relative_to((self.base_dir / "replays").resolve()):
            raise ValueError("Replay not available in this installation.")
        import webbrowser
        webbrowser.open(path.as_uri())

    def logout(self, _=None):
        with self.lock:
            if self.running:
                raise ValueError("Stop the bot before logging out.")
            self.sessions.logout()
            self.config.username = ""
            self.config.password = ""
            self.save_preferences()
        self.publish()

    def watch(self, supplied):
        tag = str(supplied.get("battle", ""))
        if not re.fullmatch(r"battle-[a-z0-9-]+", tag):
            raise ValueError("Invalid battle.")
        if tag not in self.live:
            raise ValueError("That battle has ended; open its replay from the history.")
        if self.config.server != "showdown":
            raise ValueError("Watching in the browser only works on the official server.")
        # The Showdown tab logged in with the same account also shows the room by itself.
        import webbrowser
        webbrowser.open(f"https://play.pokemonshowdown.com/{tag}")


    def handle_event(self, event):
        with self.lock:
            kind = event.get("type")
            if kind == "log":
                return
            if kind == "status":
                self.status_message = str(event.get("message", ""))
                if self.status not in {"Stopping", "Recovering"}:
                    self.status = "Waiting" if "Waiting for battle" in self.status_message else "Connecting" if "Connecting" in self.status_message else "Starting" if "Preparing" in self.status_message else self.status
            elif kind == "decision":
                self.decision = event
                # Decided by the main engine (Foul Play, or our search when it is the engine);
                # emergency heuristics and fallbacks do not count.
                mode = str(event.get("evaluation_mode", ""))
                full = mode in {"foulplay", "search-v2"} or mode.endswith("simulator")
                self.search_health["full"] += full
                self.search_health["total"] += 1
                if self.status not in {"Stopping", "Recovering"}:
                    self.status = "Playing"
                self.metrics["response_ms"] = event.get("response_ms", event.get("search", {}).get("elapsed_ms"))
            elif kind in {"live_start", "live_update"}:
                entry = dict(event.get("live") or {})
                tag = entry.get("battle")
                if tag:
                    self.live[tag] = entry
            elif kind == "live_end":
                tag = event.get("battle")
                self.live.pop(tag, None)
            elif kind in {"battle_end", "rating"}:
                self.history = self.experience.history(HISTORY_LIMIT)
            elif kind == "meta" and event.get("synced_at"):
                self.metrics["last_sync"] = event["synced_at"]
            elif kind == "error":
                self.error, self.status = str(event.get("message", "")), "Error"
            elif kind == "finished":
                self.running = False
                self.live.clear()
                self.status = "Error" if self.error else "Idle"
                self.history = self.experience.history(HISTORY_LIMIT)
                restart, self.recover_pending = self.recover_pending, False
                if restart and not self.closed.is_set():
                    self._schedule_restart()
                elif self.on_finished:
                    self.on_finished()
        self.publish()


    def _restart(self):
        try:
            self._start_recovery()
            self.publish()
        except Exception as exc:
            self.handle_event({"type": "error", "message": str(exc)})

    def _schedule_restart(self):
        if self._restart_timer:
            self._restart_timer.cancel()
        self._restart_timer = threading.Timer(.05, self._restart)
        self._restart_timer.daemon = True
        self._restart_timer.start()

    def monitor(self):
        try:
            import psutil
            psutil.cpu_percent()
        except ImportError:
            psutil = None
        while not self.closed.wait(5):
            with self.lock:
                before = dict(self.metrics)
                cpu = psutil.cpu_percent() if psutil else None
                self.metrics["cpu"] = round(cpu) if cpu is not None else None
                websocket = getattr(getattr(getattr(self.runner, "player", None), "ps_client", None), "websocket", None)
                latency = getattr(websocket, "latency", None)
                self.metrics["latency_ms"] = round(latency * 1000) if latency and self.running else None
                # Publish only meaningful changes: a full state push re-renders the UI.
                changed = (abs((self.metrics["cpu"] or 0) - (before.get("cpu") or 0)) >= 5
                           or self.metrics["latency_ms"] != before.get("latency_ms"))
            if changed and self.subscribers:
                self.publish()

    def close(self):
        self.closed.set()
        self.recover_pending = False
        if self._restart_timer:
            self._restart_timer.cancel()
            self._restart_timer = None
        if self.runner and self.runner.is_running:
            self.runner.stop()
        with self.lock:
            for mailbox in tuple(self.subscribers):
                try:
                    mailbox.put_nowait({"type": "closed"})
                except queue.Full:
                    pass


class LocalAppServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, service, port=0):
        self.service = service
        self.token = secrets.token_urlsafe(32)
        self.static = service.base_dir / "frontend/dist"
        super().__init__(("127.0.0.1", port), AppHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class AppHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def same_origin(self):
        return self.headers.get("Host") == self.server.origin.removeprefix("http://") and self.headers.get("Origin", self.server.origin) == self.server.origin

    def authorized(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        cookie_token = cookie.get("showdown-session")
        supplied = self.headers.get("X-Showdown-Token") or (cookie_token.value if cookie_token else "")
        return self.same_origin() and secrets.compare_digest(supplied, self.server.token)

    def send_bytes(self, status, body, content_type="application/json; charset=utf-8", cookie=False):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if cookie:
            self.send_header("Set-Cookie", f"showdown-session={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, status=200, cookie=False):
        self.send_bytes(status, json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), cookie=cookie)

    def do_GET(self):
        if not self.same_origin():
            self.json({"ok": False, "error": "Invalid origin."}, 403)
            return
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/session":
            self.json({"token": self.server.token}, cookie=True)
            return
        if route.startswith("/api/") and not self.authorized():
            self.json({"ok": False, "error": "Invalid session."}, 403)
            return
        try:
            if route == "/api/state":
                self.json(self.server.service.state())
            elif route == "/api/library":
                self.json({"ok": True, "teams": self.server.service.library()})
            elif route == "/api/report":
                self.json(self.server.service.report(parse_qs(parsed.query).get("battle_id", [""])[0]))
            elif route == "/api/events":
                self.events()
            elif route.startswith("/api/"):
                self.json({"ok": False, "error": "Unknown route."}, 404)
            else:
                self.serve_static(route)
        except (ValueError, OSError, TypeError) as exc:
            self.json({"ok": False, "error": str(exc)}, 400)

    def do_POST(self):
        if not self.authorized() or not secrets.compare_digest(self.headers.get("X-Showdown-Token", ""), self.server.token):
            self.json({"ok": False, "error": "Invalid session."}, 403)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 <= size <= 131072:
                raise ValueError("Request too large.")
            body = json.loads(self.rfile.read(size) or "{}")
            if not isinstance(body, dict):
                raise ValueError("Invalid request.")
            service = self.server.service
            route = urlparse(self.path).path
            if route == "/api/team/analyze":
                self.json({"ok": True, "profile": service.analyze_team(body)})
                return
            actions = {"/api/start": service.start, "/api/extra-battle": service.extra_battle, "/api/config": service.update_config, "/api/autoplay": service.set_autoplay,
                       "/api/team": service.save_team, "/api/team/delete": service.delete_team,
                       "/api/library/import": service.import_library_team, "/api/learning/reset": service.reset_learning,
                       "/api/history/reset": service.clear_history,
                       "/api/stop": lambda _: service.stop(), "/api/recover": lambda _: service.recover(),
                       "/api/open-replay": lambda value: service.open_replay(str(value.get("battle_id", ""))),
                       "/api/watch": service.watch, "/api/logout": service.logout}
            if urlparse(self.path).path not in actions:
                self.json({"ok": False, "error": "Unknown route."}, 404)
                return
            actions[urlparse(self.path).path](body)
            self.json({"ok": True, "state": service.state()})
        except (ValueError, TypeError, KeyError, OSError) as exc:
            self.json({"ok": False, "error": str(exc)}, 400)

    def serve_static(self, route):
        root = self.server.static.resolve()
        path = (root / unquote(route).lstrip("/")).resolve()
        if route == "/":
            path = root / "index.html"
        # The built frontend owns its asset versions. Legacy desktop assets are
        # only a fallback, so an older sprite index cannot shadow the new one.
        if route.startswith("/assets/") and path.is_relative_to(root) and not path.is_file():
            root = (self.server.service.base_dir / "assets").resolve()
            path = (root / unquote(route).removeprefix("/assets/")).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            self.send_bytes(404, b"Interface not found. Extract the whole package.", "text/plain; charset=utf-8")
            return
        self.send_bytes(200, path.read_bytes(), mimetypes.guess_type(path)[0] or "application/octet-stream")

    def events(self):
        service = self.server.service
        mailbox = queue.Queue(maxsize=8)
        with service.lock:
            service.subscribers.add(mailbox)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        mailbox.put({"type": "state", "state": service.state()})
        try:
            while not service.closed.is_set():
                try:
                    message = mailbox.get(timeout=10)
                    self.wfile.write(b"data: " + json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with service.lock:
                service.subscribers.discard(mailbox)


def serve(base_dir, port=0):
    service = AppService(base_dir)
    server = LocalAppServer(service, port)
    threading.Thread(target=lambda: server.serve_forever(poll_interval=.1), name="showdown-local-api", daemon=True).start()
    threading.Thread(target=service.monitor, name="showdown-metrics", daemon=True).start()
    return service, server
