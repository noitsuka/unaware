"""Connection lifecycle, autonomous player and thread-safe GUI events."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import threading
import time
import uuid
import inspect
from collections import Counter, OrderedDict
from typing import Callable
from urllib.parse import urlparse
from websockets.exceptions import ConnectionClosed

from poke_env.player import Player
from poke_env.ps_client import AccountConfiguration, ServerConfiguration
from poke_env.ps_client.server_configuration import (
    LocalhostServerConfiguration, ShowdownServerConfiguration,
)

from .engine import DecisionEngine
from .meta import MetaStore, rating_bucket, blend_preferences
from .teams import SUPPORTED_FORMATS, validate_team, parse_team
from .state import hydrate_request, hydrate_public_event
from .progress import feedback_for_search, rerank_for_progress
from .strategy import LABELS, analyze_team, analyze_opponent
from .policy import BattlePolicy
from .telemetry import BattleTrace, snapshot
from .session import SessionStore, background_call
from .client import configure_client, abort_socket
from .decision_clock import DecisionClock
from .gameplan import GamePlan

MAX_CONCURRENT = 5


def foe_switch_prior(scouted, decisions):
    """How often this opponent switches: scouting replays + this battle."""
    observed = [bool((row.get("observations") or {}).get("foe_switched")) for row in decisions[-12:]
                if not (row.get("before") or {}).get("force_switch")]
    rate = scouted.get("switch_rate") if scouted else None
    if len(observed) >= 4:
        current = sum(observed) / len(observed)
        rate = current if rate is None else .5 * rate + .5 * current
    if rate is None:
        return .45
    return max(.15, min(.9, .2 + rate))


def _make_scout(base_dir, experience):
    from .scouting import OpponentScout
    return OpponentScout(base_dir, experience)


def _apply_meta(player, meta):
    """Point every engine/search of a player at a new usage snapshot."""
    if hasattr(player, "set_meta"):
        player.set_meta(meta)
        return
    player.engine.meta = meta
    if getattr(player, "search", None) is not None:
        player.search.meta = meta


@dataclass
class BotConfig:
    username: str = ""
    password: str = ""
    format_id: str = "gen9ou"
    archetype: str = "auto"
    mode: str = "ladder"
    opponent: str = ""
    battles: int = 1
    autoplay: bool = False
    team: str = ""
    server: str = "showdown"
    websocket_url: str = ""
    authentication_url: str = ""
    risk: float = 0.35
    allow_tera: bool = True
    auto_update_meta: bool = True
    learning_enabled: bool = True
    matchmaking_timeout: float = 300.0
    concurrent_battles: int = 1
    cpu_level: str = "medium"
    # Showdown avatar set at login (/avatar), e.g. "lucas". Empty keeps the account's.
    avatar: str = ""
    # Decision engine: "foulplay" (MCTS search) or "hybrid" (the network breaks
    # Foul Play's near-ties).
    engine: str = "hybrid"

    def validate(self) -> list[str]:
        errors = []
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,17}", self.username):
            errors.append("Use a name of 1 to 18 characters (letters, digits, space, _ or -).")
        if self.format_id not in SUPPORTED_FORMATS:
            errors.append("Pick a supported Generation 9 singles format.")
        if self.archetype not in {"auto", *LABELS}:
            errors.append("Invalid team strategy.")
        if self.mode not in {"ladder", "challenge", "accept"}:
            errors.append("Invalid battle mode.")
        if self.mode == "challenge" and not self.opponent.strip():
            errors.append("Enter the user to challenge.")
        if self.opponent and not re.fullmatch(r"[A-Za-z0-9 _-]{1,18}", self.opponent):
            errors.append("Invalid opponent name.")
        if not 1 <= self.battles <= 100:
            errors.append("Choose between 1 and 100 battles per session.")
        if not 0 <= self.risk <= 1:
            errors.append("Risk must be between 0 and 1.")
        if not 10 <= self.matchmaking_timeout <= 3600:
            errors.append("The matchmaking timeout must be between 10 and 3600 seconds.")
        if type(self.concurrent_battles) is not int or not 1 <= self.concurrent_battles <= MAX_CONCURRENT:
            errors.append(f"Concurrent battles must be between 1 and {MAX_CONCURRENT}.")
        if self.mode != "ladder" and self.concurrent_battles != 1:
            errors.append("Challenges play one battle at a time; concurrent battles are for the ladder.")
        if self.cpu_level not in {"low", "medium", "high"}:
            errors.append("Invalid CPU usage level.")
        if self.engine not in {"foulplay", "hybrid"}:
            errors.append("Invalid decision engine.")
        if self.avatar and not re.fullmatch(r"[A-Za-z0-9-]{1,40}", self.avatar):
            errors.append("Invalid avatar name.")
        if self.server not in {"showdown", "local", "custom"}:
            errors.append("Invalid server.")
        if self.server == "custom":
            ws, auth = urlparse(self.websocket_url), urlparse(self.authentication_url)
            if ws.scheme not in {"ws", "wss"} or not ws.hostname:
                errors.append("Invalid WebSocket URL (ws:// or wss://).")
            if auth.scheme not in {"https", "http"} or not auth.hostname:
                errors.append("Invalid authentication URL.")
            if ws.username or ws.password or auth.username or auth.password:
                errors.append("Server URLs cannot contain credentials.")
            if self.password and auth.scheme != "https" and auth.hostname not in {"localhost", "127.0.0.1", "::1"}:
                errors.append("Sending a password to a remote server requires HTTPS authentication.")
        if self.format_id != "gen9randombattle":
            errors.extend(validate_team(self.team, self.format_id))
        return errors

    def public_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "password"}


class StrategicPlayer(Player):
    def __init__(self, *, engine: DecisionEngine, emit: Callable[[dict], None],
                 experience=None, team_id: str = "", learning_enabled: bool = True,
                 base_dir: Path | None = None, search=None, archetype="auto", rating=None,
                 session_store=None, session_config=None, recovery=None, search_pool=None,
                 engine_factory=None, scout=None, **kwargs):
        self.engine = engine
        self.search = search
        # One (engine, search) pair per simultaneous battle: engine caches and
        # search feedback are per decision and must never be shared by threads.
        self._pool = [search] + [item for item in (search_pool or []) if item is not search]
        self._engine_factory = engine_factory
        self._engines = {}
        self._slots: dict[str, int] = {}
        self._slot_lock = threading.Lock()
        self._matchup_memory = {}
        self.live: dict[str, dict] = {}
        # Opponent scouting (public replays + local history), one per room.
        self.scout = scout
        self._scouts: dict[str, dict] = {}
        self._plans: dict = {}
        self.emit = emit
        self.fallback_count = 0
        self.experience = experience
        self.team_id = team_id
        self.learning_enabled = learning_enabled
        self.current_rating = rating
        self.research_priors = {}
        self.base_dir = Path(base_dir) if base_dir else None
        self.session_id = uuid.uuid4().hex[:12]
        self.traces: dict[str, BattleTrace] = {}
        self.completed_records: dict[str, dict] = {}
        self._learning_cache: dict[tuple, tuple] = {}
        self._progress_memory = None
        self.session_store = session_store
        self.session_config = session_config
        self.recovery = recovery
        self.recovery_ready = asyncio.Event()
        self.recovery_error = ""
        self.shutting_down = False
        self._sent_requests = {}
        self._finished_tags = OrderedDict()
        self._finished_counts = Counter()
        self._room_jobs = {}
        self._room_departures = {}
        self._decision_clock = DecisionClock()
        self._room_state_lock = threading.RLock()
        self._report_jobs = set()
        self._saving_tags = set()
        self._initializing_room = False
        if recovery:
            self.session_id = recovery.get("session_id") or self.session_id
        raw_team = kwargs.get("team")
        self.own_sets = parse_team(raw_team) if isinstance(raw_team, str) and raw_team.strip() else []
        self.strategy_profile = analyze_team(self.own_sets, archetype)
        for item in self._pool:
            if item is not None:
                item.strategy_profile = self.strategy_profile
        super().__init__(**kwargs)
        self._owned_logger_handler = self.logger.handlers[-1] if self.logger.handlers else None
        if session_store:
            configure_client(self.ps_client, session_store)
        # Decision engine (3.13): Foul Play when its Rust engine is installed,
        # otherwise our own simulator search (brain v2).
        from . import foulplay_brain
        self.brain_mode = "foulplay" if foulplay_brain.available() else "v2"
        self.foulplay_search_ms = 4500  # 3000 until 3.19; more search for a 6-core CPU
        self.foulplay_engine = "foulplay"
        self.foulplay_loops = True  # battle memory that leaves loops (fp_loops); off only for A/B
        self.foulplay_endgame = True  # longer search with few Pokémon left; off only for A/B
        self.foulplay_worlds = foulplay_brain.WORLDS  # sampled worlds per decision (0 = Foul Play's own count)
        self.foulplay_guards = True  # Destiny Bond / choice-lock guards (fp_guards); off only for A/B
        self.foulplay_parallelism = foulplay_brain.default_parallelism(kwargs.get("max_concurrent_battles") or 1)
        self._team_text = raw_team if isinstance(raw_team, str) else ""
        self._fp_battles: dict = {}
        handle = self.ps_client._handle_message

        async def observed(message):
            self._observe_raw(message)
            await handle(message)
        self.ps_client._handle_message = observed
        self.refresh_preferences()

    @property
    def n_won_battles(self):
        return self._finished_counts["win"]

    @property
    def n_lost_battles(self):
        return self._finished_counts["loss"]

    @property
    def n_tied_battles(self):
        return self._finished_counts["tie"]

    @property
    def n_finished_battles(self):
        return sum(self._finished_counts.values())

    def _resources(self, tag):
        """(engine, search) reserved for one battle room."""
        pool = getattr(self, "_pool", None) or [getattr(self, "search", None)]
        if len(pool) <= 1:
            return self.engine, pool[0]
        with self._slot_lock:
            index = self._slots.get(tag)
            if index is None:
                used = set(self._slots.values())
                index = next((i for i in range(len(pool)) if i not in used), len(self._slots) % len(pool))
                self._slots[tag] = index
            engine = self.engine if index == 0 else self._engines.get(index)
            if engine is None:
                engine = self._engine_factory() if self._engine_factory else self.engine
                self._engines[index] = engine
        return engine, pool[index]

    def _release(self, tag):
        lock = getattr(self, "_slot_lock", None)
        if lock is None:
            return
        with lock:
            self._slots.pop(tag, None)

    def _lookup_ladder_rank(self, battle):
        """Both players' positions in the format's top 500 (one cached download),
        shown on the live card and kept in the history record."""
        if not getattr(self, "ladder_lookup", False) or not battle.opponent_username:
            return
        looked = self.__dict__.setdefault("_ranked_rooms", set())
        if battle.battle_tag in looked:
            return
        looked.add(battle.battle_tag)
        from .ladder_rank import lookup

        async def run():
            info = await background_call(lookup, battle.opponent_username, self.format)
            own = await background_call(lookup, self.username, self.format)
            self.__dict__.setdefault("_ladder_info", {})[battle.battle_tag] = {"foe": info, "own": own}
            if (info or own) and not battle.finished:
                self._live_update(battle, opponent_ladder=info or None, own_ladder=own or None)
        task = asyncio.create_task(run())
        self._report_jobs.add(task)
        task.add_done_callback(self._report_jobs.discard)

    def _start_scout(self, battle):
        self._lookup_ladder_rank(battle)
        scout, scouts = getattr(self, "scout", None), getattr(self, "_scouts", None)
        if scout is None or scouts is None or not battle.opponent_username or battle.battle_tag in scouts:
            return
        entry = scouts[battle.battle_tag] = {"ready": threading.Event(), "profile": {}}
        async def run():
            try:
                entry["profile"] = await background_call(scout.scout, battle.opponent_username, self.format) or {}
            except Exception as exc:
                entry["profile"] = {"games": 0, "summary": f"Scouting unavailable: {type(exc).__name__}."}
            finally:
                entry["ready"].set()
            profile = self._scout_profile(battle.battle_tag, battle=battle)
            self.emit({"type": "log", "message": f"Scouting {battle.opponent_username}: {profile.get('summary', '')}"})
            self._feed_scouting(battle.battle_tag)
            if "team_view" not in entry and not battle.finished:  # preview not in yet
                self._live_update(battle, scout={"games": profile.get("games", 0), "summary": profile.get("summary", ""),
                                                 "archetype": profile.get("archetype"), "leads": profile.get("leads", {})})
        task = asyncio.create_task(run())
        self._report_jobs.add(task)
        task.add_done_callback(self._report_jobs.discard)

    def _feed_scouting(self, tag):
        """Hand this opponent's scouted games to the room's Foul Play (set prior)."""
        bridge = (getattr(self, "_fp_battles", None) or {}).get(tag)
        entry = (getattr(self, "_scouts", None) or {}).get(tag)
        games = ((entry or {}).get("profile") or {}).get("raw_games") or []
        if bridge is not None and games and hasattr(bridge, "set_scouting"):
            bridge.set_scouting(games)
            shown = sum(1 for game in games if (game.get("team") or {}).get("revealed"))
            self.emit({"type": "log", "message": f"Scouting → Foul Play: {shown} of the opponent's games with revealed sets."})

    def _scout_profile(self, tag, wait=0., battle=None):
        """The scouted profile; once the team preview is known, narrowed to games
        with this same team (scouting.OpponentScout.for_team)."""
        entry = getattr(self, "_scouts", {}).get(tag)
        if not entry:
            return {}
        if wait:
            entry["ready"].wait(wait)
        if entry.get("team_view") is None and entry["ready"].is_set() and battle is not None:
            preview = [mon.species for mon in getattr(battle, "teampreview_opponent_team", None) or []]
            if preview:
                from .scouting import OpponentScout
                view = entry["team_view"] = OpponentScout.for_team(entry["profile"], preview)
                if not battle.finished:
                    self._live_update(battle, scout={"games": view.get("games", 0), "team_games": view.get("team_games", 0),
                                                     "summary": view.get("summary", ""), "archetype": view.get("archetype"),
                                                     "leads": view.get("leads", {}), "record": view.get("record", {})})
        return entry.get("team_view") or entry["profile"]

    def _live_update(self, battle, **fields):
        """Public summary of a running battle for the launcher's live list."""
        live = getattr(self, "live", None)
        if live is None or battle is None:
            return
        tag = battle.battle_tag
        entry = live.get(tag)
        if entry is None:
            entry = live[tag] = {"battle": tag, "format_id": self.format, "started_at": time.time(),
                                 "turn": 0, "opponent": "", "own_team": [], "foe_team": [],
                                 "rating": getattr(self, "current_rating", None)}
            fresh = True
        else:
            fresh = False
            if entry["turn"] == battle.turn and not fields and entry["foe_team"]:
                return
        entry["turn"] = battle.turn
        entry["opponent"] = battle.opponent_username or entry["opponent"]
        own = [mon.species for mon in battle.team.values()] or [mon.species for mon in getattr(battle, "teampreview_team", [])]
        foes = [mon.species for mon in getattr(battle, "teampreview_opponent_team", [])] or [
            mon.species for mon in battle.opponent_team.values()]
        entry["own_team"] = own[:6] or entry["own_team"]
        entry["foe_team"] = foes[:6] or entry["foe_team"]
        entry["own_alive"] = sum(not mon.fainted for mon in battle.team.values()) if battle.team else 6
        revealed = {mon.species: mon for mon in battle.opponent_team.values()}
        entry["foe_alive"] = max(0, len(entry["foe_team"]) - sum(mon.fainted for mon in revealed.values())) if entry["foe_team"] else 6
        entry["opponent_rating"] = (self.traces.get(tag).opponent_rating if self.traces.get(tag) else None)
        # Field for the live card: hazards/screens per side, weather and terrain.
        entry["field"] = {"own": {k.name: v for k, v in (getattr(battle, "side_conditions", None) or {}).items()},
                          "foe": {k.name: v for k, v in (getattr(battle, "opponent_side_conditions", None) or {}).items()},
                          "weather": [w.name for w in getattr(battle, "weather", None) or {}],
                          "fields": [f.name for f in getattr(battle, "fields", None) or {}]}
        entry.update(fields)
        self.emit({"type": "live_start" if fresh else "live_update", "live": dict(entry)})

    def _win_chance(self, battle):
        """Win chance for the live card, in percent (neural/winchance): calibrated
        material model every turn, nudged by the network when its estimate is
        from this turn, smoothed across turns."""
        try:
            from .neural.winchance import battle_material, combine, material_probability
            material = material_probability(*battle_material(battle))
        except Exception:
            return None
        bridge = (getattr(self, "_fp_battles", None) or {}).get(battle.battle_tag)
        info = getattr(bridge, "winprob", None) or {}
        net = info.get("value") if info.get("turn") == battle.turn else None
        previous = self.__dict__.setdefault("_win_previous", {})
        noted = self.__dict__.setdefault("_win_noted", set())
        if net is not None and battle.battle_tag not in noted:
            noted.add(battle.battle_tag)
            from .neural.winchance import note_start
            note_start(net)  # the network's typical start, to remove its bias
        from .neural.winchance import start_bias
        value = combine(material, net, previous.get(battle.battle_tag), start_bias())
        previous[battle.battle_tag] = value
        if len(previous) > 64:
            previous.pop(next(iter(previous)))
        return round(100 * value)

    def _checkpoint(self, battle):
        if not self.session_store or not self.session_config or battle.finished:
            return
        trace = self.traces.get(battle.battle_tag)
        value = self.session_config.public_dict()
        value.update(battle_tag=battle.battle_tag, session_id=self.session_id, turn=battle.turn,
                     request=battle.last_request, state=snapshot(battle),
                     sent_rqid=self._sent_requests.get(battle.battle_tag),
                     trace={"decisions": trace.decisions[-300:], "rating_before": trace.rating_before,
                            "rating_after": trace.rating_after, "opponent_rating": trace.opponent_rating} if trace else {})
        try:
            self.session_store.save(value)
        except OSError as exc:
            # A recovery checkpoint must never cost the turn: 9 requests in one
            # session raised PermissionError here and no move was sent.
            self.emit({"type": "log", "message": f"Recovery checkpoint not saved ({type(exc).__name__}); the battle goes on."})

    async def _handle_battle_request(self, battle, maybe_default_order=False):
        # A rejoin's init log is parsed fully before the private live request.
        if self.shutting_down or self._initializing_room or battle.finished:
            return
        request = battle.last_request
        rqid = request.get("rqid")
        if battle._wait or request.get("wait"):
            self._waiting.set()
            self.recovery_ready.set()
            self._checkpoint(battle)
            return
        if rqid is not None and self._sent_requests.get(battle.battle_tag) == rqid and not maybe_default_order:
            return
        self._checkpoint(battle)
        chooser = self.teampreview if battle.teampreview else self.choose_move
        choice = await background_call(chooser, battle)
        if inspect.isawaitable(choice):
            choice = await choice
        if self.shutting_down or battle.finished or battle.last_request.get("rqid") != rqid:
            return
        message = choice if isinstance(choice, str) else choice.message
        if message:
            # rqid guards against a stale choice reaching a newer turn.
            await self.ps_client.send_message(message, battle.battle_tag, str(rqid) if rqid is not None else None)
            self._sent_requests[battle.battle_tag] = rqid
            self._checkpoint(battle)
        self.recovery_ready.set()

    def refresh_preferences(self):
        if not self.experience or not self.learning_enabled:
            self._matchup_memory = {}
        else:
            loader = getattr(self.experience, "matchup_memory", None)
            try:
                self._matchup_memory = loader(self.format, self.team_id) if loader else {}
            except Exception as exc:
                self._matchup_memory = getattr(self, "_matchup_memory", {})
                self.emit({"type": "log", "message": f"Matchup memory unavailable; keeping the last valid one: {exc}"})
        pool = [item for item in (getattr(self, "_pool", None) or [getattr(self, "search", None)]) if item is not None]
        if not pool:
            return
        local = self.experience.preferences(self.format, rating=self.current_rating,
                    username=self.username, server_namespace=urlparse(self.ps_client.websocket_url).netloc).get("species", {}) if self.experience and self.learning_enabled else {}
        preferences = blend_preferences(local, self.research_priors if self.learning_enabled else None)
        for item in pool:
            item.preferences = preferences

    async def _handle_battle_message(self, split_messages):
        tag = split_messages[0][0].lstrip(">")
        messages = split_messages[1:]
        if self.shutting_down:
            return
        clock = getattr(self, "_decision_clock", None)
        if clock is not None:
            clock.observe(tag, messages, self.username)
        if any(len(m) > 1 and m[1] in {"deinit", "noinit"} for m in messages):
            departure = self._room_departures.get(tag)
            if departure is not None:
                departure.set()
                self.emit({"type": "room_closed", "battle": tag, "confirmed": True,
                           "scope": "connection"})
            if self.recovery and tag == self.recovery.get("battle_tag") and not self.recovery_ready.is_set():
                self.recovery_error = "The saved room is not available for recovery."
                self.recovery_ready.set()
            if tag in self._finished_tags:
                self._cleanup_room(tag, keep_lock=True)
            return
        if tag in self._finished_tags and tag not in self.traces:
            return
        initializing = any(len(m) > 1 and m[1] == "init" for m in messages)
        sentchoice = any(len(m) > 2 and m[1] == "sentchoice" and m[2] for m in messages)
        trace = self.traces.setdefault(tag, BattleTrace())
        recovering = initializing and self.recovery and tag == self.recovery.get("battle_tag")
        if recovering:
            saved = self.recovery.get("trace", {})
            trace.decisions = list(saved.get("decisions", []))
            trace.rating_before, trace.rating_after = saved.get("rating_before"), saved.get("rating_after")
            trace.opponent_rating = saved.get("opponent_rating")
        self._initializing_room = bool(initializing or sentchoice)
        try:
            for message in messages:
                kind = message[1] if len(message) > 1 else ""
                battle = self.battles.get(tag)
                # A recovered full log reconstructs state, not new experience.
                if not recovering:
                    trace.observe([message], self.username, battle.player_role if battle else None)
                if kind == "sentchoice":
                    continue
                if kind in {"win", "tie"}:
                    if tag in self._finished_tags or battle is None:
                        continue
                    battle.won_by(message[2]) if kind == "win" else battle.tied()
                    result = "win" if battle.won else "loss" if battle.lost else "tie"
                    self._finished_counts[result] += 1
                    live = getattr(self, "live", {}).pop(tag, None)
                    self.emit({"type": "live_end", "battle": tag, "result": result,
                               "live": live or {"battle": tag}})
                    self._finished_tags[tag] = None
                    if len(self._finished_tags) > 128:
                        self._finished_tags.popitem(last=False)
                    # /leave only affects this connection. A completed send is
                    # not acknowledgement: retain a bounded retry until deinit.
                    self._room_departures[tag] = asyncio.Event()
                    await self._send_room_leave(tag)
                    self._room_jobs[tag] = asyncio.create_task(self._release_room_later(tag))
                    if self.session_store:
                        self.session_store.clear(tag)
                    if recovering:
                        self.recovery_error = "The saved battle has already ended; no new battle was started."
                    self.recovery_ready.set()
                    self._battle_finished_callback(battle)
                    try:
                        self._battle_count_queue.get_nowait()
                        self._battle_count_queue.task_done()
                    except asyncio.QueueEmpty:
                        pass
                    async with self._battle_end_condition:
                        self._battle_end_condition.notify_all()
                    continue
                await super()._handle_battle_message([split_messages[0], message])
                hydrate_public_event(self.battles.get(tag), message)
        finally:
            self._initializing_room = False
        battle = self.battles.get(tag)
        if battle and not battle.finished:
            self._start_scout(battle)
            self._live_update(battle)
            if sentchoice:
                self._sent_requests[tag] = battle.last_request.get("rqid")
                self.recovery_ready.set()
            elif initializing and battle.last_request:
                await self._handle_battle_request(battle)
            self._checkpoint(battle)
        record = self.completed_records.get(tag)
        if record and trace.rating_after is not None and record.get("rating_after") != trace.rating_after:
            record.update(rating_before=trace.rating_before, rating_after=trace.rating_after)
            if self.experience:
                try:
                    await background_call(self.experience.update_rating, record["battle_id"], record["server_namespace"], trace.rating_before, trace.rating_after)
                except Exception as exc:
                    self.emit({"type": "log", "message": f"Could not save the received Elo: {exc}"})
            self.emit({"type": "battle_end", "record": record})
            self.emit({"type": "rating", "format_id": self.format, "username": self.username, "rating": trace.rating_after})
            self.current_rating = trace.rating_after
            # Reads the battle store (seconds on a long history): keep it off the battle loop.
            await background_call(self.refresh_preferences)

    async def _send_room_leave(self, tag):
        try:
            await asyncio.wait_for(self.ps_client.send_message(f"/leave {tag}"), .35)
        except (asyncio.TimeoutError, OSError, ConnectionClosed) as exc:
            self.emit({"type": "log", "message":
                       f"Leaving {tag} not sent yet ({type(exc).__name__}); retrying automatically."})

    async def _release_room_later(self, tag):
        departure = self._room_departures[tag]
        try:
            for attempt, delay in enumerate((.65, 1.0, 1.4)):
                if self.shutting_down:
                    return
                try:
                    await asyncio.wait_for(departure.wait(), delay)
                    return
                except asyncio.TimeoutError:
                    if attempt < 2:
                        await self._send_room_leave(tag)
            self.emit({"type": "log", "message":
                       f"The server did not confirm leaving {tag} after 3 attempts. "
                       "The result was kept; the connection closes when the session ends."})
            self.emit({"type": "room_closed", "battle": tag, "confirmed": False,
                       "scope": "connection"})
        finally:
            if tag in self._room_departures:
                self._cleanup_room(tag)

    def _cleanup_room(self, tag, keep_lock=False):
        if getattr(self, "_decision_clock", None) is not None:
            self._decision_clock.clear(tag)
        self._battles.pop(tag, None)
        with self._room_state_lock:
            self.traces.pop(tag, None)
            self.completed_records.pop(tag, None)
        self._sent_requests.pop(tag, None)
        self._room_departures.pop(tag, None)
        for engine in (self.engine, *getattr(self, "_engines", {}).values()):
            getattr(engine, "_last_switch", {}).pop(tag, None)
        getattr(self, "live", {}).pop(tag, None)
        getattr(self, "_scouts", {}).pop(tag, None)
        getattr(self, "_plans", {}).pop(tag, None)
        self._release(tag)
        if not keep_lock:
            self.ps_client._battle_locks.pop(tag, None)
        job = self._room_jobs.pop(tag, None)
        if job and job is not asyncio.current_task():
            job.cancel()

    def choose_move(self, battle):
        began = time.monotonic()
        try:
            if getattr(self, "shutting_down", False):
                return self.choose_default_move()
            hydrate_request(battle, getattr(self, "own_sets", ()))
            trace = self.traces.setdefault(battle.battle_tag, BattleTrace())
            trace.close(battle)
            state = snapshot(battle)
            opponent_profile = analyze_opponent(battle)
            engine, evaluator = self._resources(battle.battle_tag)
            scouted = self._scout_profile(battle.battle_tag, battle=battle)
            if evaluator is not None:
                evaluator.opponent_scout = scouted
                for key, value in (getattr(self, "search_overrides", None) or {}).items():
                    setattr(evaluator, key, value)  # A/B experiments only
                from .gameplan import inferred_choice_lock, observed_habit
                from .strategy import base_species_id
                foe = battle.opponent_active_pokemon
                lock = inferred_choice_lock(trace.decisions, foe)
                evaluator.foe_switch_prior = foe_switch_prior(scouted, trace.decisions)
                evaluator.choice_locked = {base_species_id(foe.species): lock} if lock and foe else {}
                evaluator.foe_habit = observed_habit(trace.decisions, foe, battle.active_pokemon)
            if scouted.get("archetype", "unknown") != "unknown" and opponent_profile.get("confidence", 0) < scouted.get("archetype_confidence", 0):
                opponent_profile = {**opponent_profile, "archetype": scouted["archetype"],
                                    "confidence": scouted["archetype_confidence"], "source": "scouting"}
            candidates = engine.rank_actions(battle)
            if not candidates:
                raise ValueError("No option was evaluated for this request.")
            maximum_budget = getattr(evaluator, "time_budget_ms", 7000)
            clock = getattr(self, "_decision_clock", None)
            turn_budget = clock.budget(battle.battle_tag, maximum_budget) if clock else maximum_budget
            evaluation_mode = "heuristic"
            # Foul Play decides first (3.13); our own search is the fallback.
            fp_choice = self._foulplay_choice(battle, candidates, turn_budget) if len(candidates) > 1 else None
            memory = {}
            if self.learning_enabled and self.experience:
                try:
                    if getattr(self, "_progress_memory", None) is None:
                        self._progress_memory = self.experience.progress_memory(self.format, self.team_id)
                    memory = self._progress_memory
                except Exception as exc:
                    self.emit({"type": "log", "message": f"Progress memory unavailable: {exc}"})
            if evaluator is not None and turn_budget < 200:
                evaluator.last_diagnostics = {"skipped": "clock", "time_budget_ms": turn_budget}
                self.emit({"type": "log", "message": "Low clock: deciding immediately with the local tactical evaluation."})
            # A single legal option (last Pokémon to send in) needs no search:
            # answering at once keeps our battle timer for the turns that matter.
            if evaluator is not None and turn_budget >= 200 and len(candidates) > 1 and fp_choice is None:
                from .search import SearchUnavailable
                try:
                    evaluator.feedback = feedback_for_search(state, trace.decisions, memory)
                    evaluator.opponent_profile = opponent_profile
                    evaluator.time_budget_ms = turn_budget
                    # Under timer pressure the clock already cut the budget:
                    # no overrun allowed then.
                    evaluator.grace_ms = 2500 if turn_budget >= maximum_budget else 0
                    candidates = evaluator.rank(battle, candidates, engine)
                    evaluation_mode = "simulator"
                except SearchUnavailable as exc:
                    if getattr(self, "shutting_down", False):
                        return self.choose_default_move()
                    self.fallback_count += 1
                    self.emit({"type": "status", "message": "Simulator unavailable this turn; using the emergency evaluation."})
                    self.emit({"type": "log", "message": f"Search interrupted on turn {battle.turn}: {exc}"})
                finally:
                    evaluator.time_budget_ms = maximum_budget
            profile = getattr(self, "strategy_profile", {})
            if not getattr(self, "own_sets", None):
                profile = analyze_team(list(battle.team.values()))
            brain_v2 = evaluation_mode == "simulator" and getattr(evaluator, "brain", "legacy") == "v2"
            policy = BattlePolicy()
            for key, value in (getattr(self, "policy_overrides", None) or {}).items():
                setattr(policy, key, value)  # A/B experiments only
            if fp_choice is not None:
                from .sure_ko import override as sure_ko
                finisher = sure_ko(battle, fp_choice, candidates)
                if finisher is not None:
                    self.emit({"type": "log", "message": f"Turn {battle.turn}: {finisher.label} surely knocks out "
                               f"{battle.opponent_active_pokemon.species}; Foul Play wanted {fp_choice.label}."})
                    fp_choice = finisher
                candidates = [fp_choice] + [c for c in candidates if c is not fp_choice]
                evaluation_mode = "foulplay"
                self._win_plan(battle, engine, profile, scouted)
            elif brain_v2:
                # Brain v2 (3.13): the official-simulator search decides. The
                # stacked hand-made layers below (feature weights, game plan,
                # progress rerank) overrode it: in the 0-6 loss of 14/10 they
                # chose Volt Switch at the search's worst value (-197 vs -167).
                candidates = sorted(candidates, key=lambda c: (-c.score, c.terastallize, c.label))
                evaluation_mode = "search-v2"
                self._win_plan(battle, engine, profile, scouted)
            else:
                candidates = policy.rank(battle, candidates, engine, profile, opponent_profile)
                evaluation_mode = "probabilistic+" + evaluation_mode
                plan = self._win_plan(battle, engine, profile, scouted) if getattr(self, "use_winplan", True) else None
                candidates = GamePlan(principles=getattr(self, "use_principles", True), refined=getattr(self, "use_refined", True)).apply(battle, candidates, profile, opponent_profile, trace.decisions, state,
                                              getattr(self, "_matchup_memory", {}) if self.learning_enabled else {},
                                              winplan=plan)
                candidates = rerank_for_progress(state, candidates, trace.decisions, memory)
            chosen = candidates[0]
            engine.record_choice(battle, chosen)
            self._live_update(battle, win_chance=self._win_chance(battle))
            trace.begin(battle, candidates, chosen, state)
            self.emit({
                "type": "decision", "battle": battle.battle_tag, "turn": battle.turn,
                "chosen": chosen.label, "evaluation_mode": evaluation_mode,
                "strategy": {"archetype": profile.get("archetype"), "win_conditions": profile.get("win_conditions", [])},
                "opponent_strategy": {"archetype": opponent_profile.get("archetype"), "confidence": opponent_profile.get("confidence"), "incomplete": True},
                "search": evaluator.last_diagnostics if evaluator is not None else {},
                "response_ms": round((time.monotonic() - began) * 1000),
                "candidates": [{"label": c.label, "score": round(c.score, 3),
                                "reasons": c.reasons} for c in candidates[:12]],
            })
            return self.create_order(chosen.action, terastallize=chosen.terastallize)
        except Exception as exc:
            # A protocol/default choice keeps a battle alive; it is always disclosed.
            self.fallback_count += 1
            self.emit({"type": "log", "message": f"Turn {battle.turn}: server default move as fallback: {type(exc).__name__}: {exc}"})
            return self.choose_default_move()

    def teampreview(self, battle):
        try:
            hydrate_request(battle, getattr(self, "own_sets", ()))
            engine, evaluator = self._resources(battle.battle_tag)
            if evaluator is not None:
                for key, value in (getattr(self, "search_overrides", None) or {}).items():
                    setattr(evaluator, key, value)  # A/B experiments only
                # Give scouting a few seconds: the usual lead shapes our lead.
                evaluator.opponent_scout = self._scout_profile(battle.battle_tag, wait=6., battle=battle)
            fp_team = self._foulplay_wait(battle, 40.)
            if fp_team and fp_team.startswith("/team "):
                digits = fp_team.split()[1]
                if sorted(int(d) for d in digits if d.isdigit()) == list(range(1, len(battle.team) + 1)):
                    self._live_update(battle, win_chance=self._win_chance(battle))
                    self.emit({"type": "log", "message": f"Foul Play's lead: {list(battle.team.values())[int(digits[0]) - 1].species}."})
                    return "/team " + digits
            order = evaluator.preview_order(battle, engine) if evaluator is not None else engine.preview_order(battle)
            if sorted(order) != list(range(1, len(battle.team) + 1)):
                raise ValueError("Incomplete team preview order.")
            roster = list(battle.team.values())
            order = self._plan_lead(battle, roster, order)
            self._win_plan(battle, engine, getattr(self, "strategy_profile", {}) or {}, self._scout_profile(battle.battle_tag, battle=battle))
            for slot in order:
                roster[slot - 1]._selected_in_teampreview = True
            self.emit({"type": "log", "message": f"Lead: {roster[order[0] - 1].species}."})
            return "/team " + "".join(map(str, order))
        except Exception as exc:
            self.fallback_count += 1
            self.emit({"type": "log", "message": f"Default team preview: {exc}"})
            order = self.engine.preview_order(battle)
            return "/team " + "".join(map(str, order)) if order else self.random_teampreview(battle)

    def set_meta(self, meta):
        self.engine.meta = meta
        for engine in getattr(self, "_engines", {}).values():
            engine.meta = meta
        for item in getattr(self, "_pool", None) or [self.search]:
            if item is not None:
                item.meta = meta

    def _observe_raw(self, message: str) -> None:
        """Every raw battle-room message also goes to that room's Foul Play."""
        if getattr(self, "brain_mode", "v2") != "foulplay" or not message.startswith(">battle-"):
            return
        tag = message.split(chr(10), 1)[0][1:].strip()
        bridge = self._fp_battles.get(tag)
        if bridge is None:
            if tag in self._fp_battles or "|init|battle" not in message or not self._team_text:
                return  # rejoined mid-battle, or unavailable: our own search plays this one
            from . import foulplay_brain
            try:
                foulplay_brain.configure(self.username, self.format, self.foulplay_search_ms, self.foulplay_parallelism)
                bridge = foulplay_brain.FoulPlayBattle(tag, self.username, self.format, self._team_text,
                                                       self.foulplay_search_ms, self.foulplay_parallelism,
                                                       engine=self.foulplay_engine,
                                                       loops=self.foulplay_loops, endgame=self.foulplay_endgame,
                                                       worlds=self.foulplay_worlds, guards=self.foulplay_guards)
            except Exception as exc:
                self.emit({"type": "log", "message": f"Foul Play unavailable for this battle: {type(exc).__name__}: {exc}"})
                self._fp_battles[tag] = None
                return
            self._fp_battles[tag] = bridge
            self._feed_scouting(tag)  # scouting may have finished first
        bridge.feed(message)
        lines = message.split(chr(10))
        if any(line.startswith("|win|") or line == "|tie" or line.startswith("|tie|") for line in lines):
            # Foul Play's own loop returns on the result line; stopping its
            # event loop here raised a spurious "Event loop stopped" failure.
            self._fp_battles.pop(tag, None)

    def _foulplay_wait(self, battle, seconds: float):
        bridge = self._fp_battles.get(battle.battle_tag) if getattr(self, "brain_mode", "v2") == "foulplay" else None
        if bridge is None:
            return None
        message = bridge.decision((battle.last_request or {}).get("rqid"), seconds)
        if message is None:
            reason = bridge.error or "sem resposta no prazo"
            self.emit({"type": "log", "message": f"Turn {battle.turn}: Foul Play did not decide ({reason}); using the fallback search."})
        return message

    def _foulplay_choice(self, battle, candidates, turn_budget_ms):
        """The candidate matching Foul Play's command for this request, or None."""
        # Foul Play sizes its search from the battle clock (foulplay_brain.clock_ms):
        # wait up to the real turn deadline minus a margin, not a fixed time.
        clock = getattr(self, "_decision_clock", None)
        deadline_s = clock.budget(battle.battle_tag, 120_000) / 1000 if clock else 0
        wait = deadline_s - 4 if deadline_s > 10 else turn_budget_ms / 1000 + 12
        message = self._foulplay_wait(battle, max(4., min(60., wait)))
        if not message:
            return None
        from poke_env.battle import Move, Pokemon
        from .strategy import base_species_id
        parts = message.split()
        if parts[0] == "/switch" and len(parts) > 1 and parts[1].isdigit():
            side = ((battle.last_request or {}).get("side") or {}).get("pokemon") or []
            index = int(parts[1]) - 1
            if 0 <= index < len(side):
                wanted = base_species_id(str(side[index].get("details", "")).split(",")[0])
                for candidate in candidates:
                    action = candidate.action
                    if isinstance(action, Pokemon) and base_species_id(action.species) == wanted:
                        return candidate
        elif parts[:2] == ["/choose", "move"] and len(parts) > 2:
            move_id = "".join(c for c in parts[2].lower() if c.isalnum())
            tera = "terastallize" in parts
            for candidate in candidates:
                action = candidate.action
                if isinstance(action, Move) and action.id == move_id and bool(candidate.terastallize) == tera:
                    return candidate
        self.emit({"type": "log", "message": f"Turn {battle.turn}: Foul Play's move has no match ({message}); using the fallback search."})
        return None

    def _win_plan(self, battle, engine, profile, scouted=None):
        """Win conditions of both sides, refreshed when information changes."""
        from .winplan import WinPlan
        plans = getattr(self, "_plans", None)
        if plans is None:
            return None
        plan = plans.setdefault(battle.battle_tag, WinPlan())
        before = plan.summary()
        try:
            plan.refresh(battle, engine, profile, scouted)
        except Exception as exc:
            self.emit({"type": "log", "message": f"Win plan unavailable: {type(exc).__name__}: {exc}"})
            return None
        summary = plan.summary()
        if summary and summary != before:
            self.emit({"type": "log", "message": f"{battle.battle_tag}: {summary}"})
        return plan

    def _plan_lead(self, battle, roster, order):
        """HO leads with its field setter unless the preview shows Magic Bounce."""
        from .strategy import base_species_id
        profile = getattr(self, "strategy_profile", {}) or {}
        style = profile.get("archetype")
        if style not in {"hyper_offense", "weather"}:
            return order
        roles = {base_species_id(row["species"]): set(row.get("roles", [])) for row in profile.get("roles", [])}
        setters = {base_species_id(species) for species in profile.get("weather_setters", [])}
        def lead_value(mon):
            names = roles.get(base_species_id(mon.species), set())
            if style == "weather":
                # Weather teams open with the automatic setter (Drizzle etc.).
                return 3 * (base_species_id(mon.species) in setters and "weather_setter" in names)
            return 3 * ("suicide_lead" in names) + 2 * ("web_setter" in names) + 2 * ("screens" in names)
        best = max(range(len(roster)), key=lambda i: (lead_value(roster[i]), -order.index(i + 1)))
        if lead_value(roster[best]) == 0 or order[0] == best + 1:
            return order
        foes = getattr(battle, "teampreview_opponent_team", []) or []
        bounce = any("magicbounce" in {str(a) for a in (getattr(mon, "possible_abilities", None) or [])} for mon in foes)
        if bounce and style == "hyper_offense":
            return order
        return [best + 1] + [slot for slot in order if slot != best + 1]

    def _battle_finished_callback(self, battle):
        if battle.battle_tag in self._saving_tags or battle.battle_tag in self.completed_records:
            return
        trace = self.traces.setdefault(battle.battle_tag, BattleTrace())
        trace.close(battle)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._persist_finished(battle, trace)
            return
        self._saving_tags.add(battle.battle_tag)
        job = asyncio.create_task(background_call(self._persist_finished, battle, trace))
        self._report_jobs.add(job)
        job.add_done_callback(self._report_jobs.discard)
        job.add_done_callback(lambda _: self._saving_tags.discard(battle.battle_tag))

    def _persist_finished(self, battle, trace):
        result = "win" if battle.won else "loss" if battle.lost else "tie"
        self.emit({"type": "log", "message": f"{battle.battle_tag}: {result} em {battle.turn} turnos."})
        self.emit({"type": "status", "message": f"{self.n_won_battles} wins • {self.n_lost_battles} losses • {self.n_tied_battles} ties"})
        if battle.battle_tag in self.completed_records:
            return
        if not self.experience:
            return
        record = {
            "battle_id": battle.battle_tag + ":" + self.session_id, "battle_tag": battle.battle_tag,
            "server_namespace": urlparse(self.ps_client.websocket_url).netloc,
            "username": self.username, "format_id": self.format, "team_id": self.team_id,
            "ended_at": datetime.now(timezone.utc).isoformat(), "opponent": battle.opponent_username or "",
            "result": "win" if battle.won else "loss" if battle.lost else "tie", "turns": battle.turn,
            "rating_before": trace.rating_before, "rating_after": trace.rating_after, "replay_path": None,
            "opponent_rating": trace.opponent_rating,
            "opponent_pokemon": list(dict.fromkeys(mon.species for mon in battle.opponent_team.values())),
        }
        ladder = self.__dict__.get("_ladder_info", {}).pop(battle.battle_tag, None) or {}
        for side, key in (("foe", "opponent_rank"), ("own", "own_rank")):
            if ladder.get(side):  # {} = ladder unreadable: leave the field out
                record[key] = ladder[side].get("rank")
        try:
            if self.base_dir:
                replay_path = self.base_dir / "replays" / f"{battle.battle_tag}-{self.session_id}.html"
                replay_path.parent.mkdir(parents=True, exist_ok=True)
                battle.save_replay(replay_path)
                record["replay_path"] = str(replay_path.resolve())
            report = self.experience.record_battle(record, trace.decisions, learning_enabled=self.learning_enabled)
            record.update(analysis_path=report.get("analysis_path"), summary=report.get("summary", ""))
            if trace.rating_after is not None and trace.rating_after != record["rating_after"]:
                self.experience.update_rating(record["battle_id"], record["server_namespace"], trace.rating_before, trace.rating_after)
                record.update(rating_before=trace.rating_before, rating_after=trace.rating_after)
            # A |deinit| may have released the room while the file write ran.
            # The database/event remain, but never resurrect an in-memory room.
            with self._room_state_lock:
                if battle.battle_tag in self.traces:
                    self.completed_records[battle.battle_tag] = record
            self._learning_cache.clear()
            self._progress_memory = None
            if getattr(self, "search", None) is not None and self.learning_enabled:
                self.current_rating = trace.rating_after if trace.rating_after is not None else self.current_rating
                self.refresh_preferences()
            self.emit({"type": "battle_end", "record": record})
            if battle.lost:
                self.emit({"type": "learning", "message": report.get("summary", "Loss analysed and recorded."), "report": report})
        except Exception as exc:
            self.emit({"type": "log", "message": f"Could not save the analysis: {type(exc).__name__}: {exc}"})


class _LogBridge(logging.Handler):
    def __init__(self, emit, on_failure):
        super().__init__(logging.WARNING)
        self.emit_event, self.on_failure = emit, on_failure

    def emit(self, record):
        message = record.getMessage()
        # The PS client uses INFO for raw protocol/auth traffic. Never relay it.
        self.emit_event({"type": "log", "message": message[:3000]})
        if "Popup message received:" in message or record.levelno >= logging.ERROR:
            self.on_failure(message[:1000])


class BotRunner:
    def __init__(self, config: BotConfig, event_callback: Callable[[dict], None], base_dir: Path):
        self.config = config
        self.callback = event_callback
        self.base_dir = Path(base_dir)
        self._thread = None
        self._stop = threading.Event()
        self._abort = threading.Event()
        self._loop = None
        self._main_task = None
        self._session_search = None
        self.sessions = SessionStore(self.base_dir)
        self.recovery = None
        self._failure = None
        self._log_path = None
        self._log_lock = threading.Lock()
        self.player = None
        # "+ New battle" (extra ladder battle next to the running ones).
        self._bonus = 0
        self._extra_pending = False
        self._search_factory = None

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, event):
        if self._abort.is_set() and event.get("type") in {"error", "log", "decision", "meta"}:
            return
        event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
        # Passwords are never serialized; redact accidental echoes from error text.
        if self.config.password:
            def redact(value):
                if isinstance(value, str):
                    return value.replace(self.config.password, "[redacted]")
                if isinstance(value, list):
                    return [redact(item) for item in value]
                if isinstance(value, dict):
                    return {key: redact(item) for key, item in value.items()}
                return value
            # Keep routing fields intact; redact only user-facing payloads.
            event = {key: (value if key in {"type", "timestamp"} else redact(value))
                     for key, value in event.items()}
        payload = json.dumps(event, ensure_ascii=False, default=str)
        if self._log_path:
            try:
                with self._log_lock, self._log_path.open("a", encoding="utf-8") as log:
                    log.write(payload + "\n")
            except OSError:
                pass
        try:
            self.callback(event)
        except Exception:
            logging.getLogger(__name__).exception("Failed to deliver an event to the interface")

    def start(self):
        if self.is_running:
            raise RuntimeError("A session is already running.")
        errors = self.config.validate()
        if errors:
            raise ValueError("\n".join(errors))
        self._stop.clear()
        self._abort.clear()
        self._failure = None
        self.player = None
        logs = self.base_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self._log_path = logs / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
        self._thread = threading.Thread(target=self._worker, name="showdown-session", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._abort.set()
        self.config.autoplay = False
        for item in (self._session_search, *getattr(self, "_extra_searches", ())):
            if item is not None:
                item.abort()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._cancel_now)
        self._emit({"type": "status", "message": "Closing the connection and cancelling pending actions."})

    def _cancel_now(self):
        if self.player:
            self.player.shutting_down = True
            abort_socket(self.player.ps_client)
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()

    def request_extra_battle(self):
        """Starts one more ladder battle now, next to the ones in progress."""
        if self.config.mode != "ladder":
            raise ValueError("Extra battles are ladder only.")
        loop, player = self._loop, self.player
        if loop is None or loop.is_closed() or player is None or self._stop.is_set():
            raise ValueError("No session is running.")
        active = [tag for tag, battle in player.battles.items() if not battle.finished]
        if not active:
            raise ValueError("No battle in progress: the bot is already searching for one.")
        if len(active) + self._bonus >= MAX_CONCURRENT:
            raise ValueError(f"Limit of {MAX_CONCURRENT} concurrent battles.")
        if self._extra_pending:
            raise ValueError("Already searching for the extra battle.")
        self._extra_pending = True
        asyncio.run_coroutine_threadsafe(self._extra_battle(), loop)

    async def _ensure_searches(self, player, count):
        """Our own search (the fallback) is not shared between rooms: one per battle."""
        pool = player._pool
        while len(pool) < min(count, MAX_CONCURRENT) and self._search_factory is not None:
            item = self._search_factory()
            await background_call(item.ready)
            item.strategy_profile = player.strategy_profile
            pool.append(item)
            self._extra_searches.append(item)

    async def _extra_battle(self):
        player, cfg = self.player, self.config
        try:
            active = [tag for tag, battle in player.battles.items() if not battle.finished]
            await self._ensure_searches(player, len(active) + 1)
            if cfg.concurrent_battles > 1:
                # The ladder pool searches whenever a slot is free: one more slot.
                self._bonus += 1
                self._emit({"type": "status", "message": "Extra battle: searching for an opponent..."})
                return
            self._emit({"type": "status", "message": f"Extra battle: searching for an opponent • {len(active)} in progress..."})
            known = set(player.battles)
            await player.ps_client.search_ladder_game(cfg.format_id, player.next_team)
            began = time.monotonic()
            while not self._stop.is_set():
                if set(player.battles) - known:
                    self._emit({"type": "status", "message": f"Extra battle started • {len(active) + 1} in progress."})
                    return
                if time.monotonic() - began > cfg.matchmaking_timeout:
                    break
                await asyncio.sleep(.2)
            await player.ps_client.send_message("/cancelsearch")
            if not self._stop.is_set():
                self._emit({"type": "log", "message": "Extra battle: no opponent found in time."})
        except Exception as exc:
            self._emit({"type": "log", "message": f"Extra battle failed: {type(exc).__name__}: {exc}"})
        finally:
            self._extra_pending = False

    def set_avatar(self, avatar: str):
        """Applies a new Showdown avatar to the connected account right away."""
        self.config.avatar = avatar
        loop, player = self._loop, self.player
        if avatar and player is not None and loop is not None and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(player.ps_client.change_avatar(avatar), loop)

    def set_autoplay(self, enabled: bool):
        if type(enabled) is not bool:
            raise ValueError("Autoplay must be on or off.")
        if enabled and self._stop.is_set():
            raise ValueError("The session is finishing its battle. Start again once it ends.")
        self.config.autoplay = enabled
        if not enabled:
            self._stop.set()
            self._emit({"type": "status", "message": "Autoplay off; finishing the current battle only."})

    def _worker(self):
        try:
            asyncio.run(self._run())
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._emit({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
        finally:
            player = self.player
            self._emit({"type": "finished", "wins": player.n_won_battles if player else 0,
                        "losses": player.n_lost_battles if player else 0,
                        "ties": player.n_tied_battles if player else 0,
                        "total": player.n_finished_battles if player else 0,
                        "fallbacks": player.fallback_count if player else 0})

    def _server(self):
        if self.config.server == "showdown":
            return ShowdownServerConfiguration
        if self.config.server == "local":
            return LocalhostServerConfiguration
        return ServerConfiguration(self.config.websocket_url, self.config.authentication_url)

    async def _run(self):
        # Own the worker for the whole lifecycle, including validation and
        # Player construction failures that happen before a WebSocket exists.
        self._session_search = None
        self._extra_searches = []
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        try:
            await self._run_session()
        finally:
            for item in (self._session_search, *getattr(self, "_extra_searches", ())):
                if item is None:
                    continue
                try:
                    await asyncio.wait_for(background_call(item.close), .3)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass

    async def _run_session(self):
        cfg = self.config
        store = MetaStore(self.base_dir / "cache", timeout=3.0)
        from .learning import ExperienceStore, team_fingerprint
        experience = ExperienceStore(self.base_dir)
        namespace = urlparse(self._server().websocket_url).netloc
        def same_server(record):
            source = str(record.get("server_namespace", ""))
            if cfg.server == "showdown":
                return source == "showdown" or (urlparse("//" + source).hostname or "").endswith(".psim.us")
            return source == namespace
        known_rating = next((record["rating_after"] for record in experience.history(100)
            if record.get("format_id") == cfg.format_id and str(record.get("username", "")).casefold() == cfg.username.casefold()
            and same_server(record) and record.get("rating_after") is not None), None)
        cutoff = rating_bucket(known_rating)
        meta = store.load(cfg.format_id, cutoff)
        async def refresh_meta(target):
            while not self._stop.is_set():
                try:
                    fresh = await background_call(store.update, cfg.format_id, target, 8.0)
                    if self.player and rating_bucket(getattr(self.player, "current_rating", known_rating)) == target:
                        _apply_meta(self.player, fresh)
                    self._emit({"type": "meta", "message": f"Smogon {fresh.month}, cutoff {fresh.rating}: {fresh.source_url}", "synced_at": fresh.downloaded_at})
                except Exception as exc:
                    self._emit({"type": "log", "message": f"Meta not updated: {exc}. Using the cached data or general estimates."})
                desired = rating_bucket(getattr(self.player, "current_rating", known_rating))
                if desired == target:
                    break
                # Finish the old download before starting another request on
                # this store; a late old population can never overwrite it.
                target = desired
        if meta:
            self._emit({"type": "meta", "message": f"Smogon {meta.month}, cutoff {meta.rating}: {meta.source_url}", "synced_at": meta.downloaded_at})
            for warning in meta.warnings:
                self._emit({"type": "log", "message": warning})
        else:
            self._emit({"type": "meta", "message": "No meta data for this format; using revealed information and general estimates."})
        if self._stop.is_set():
            return
        replay_dir = self.base_dir / "replays"
        replay_dir.mkdir(parents=True, exist_ok=True)
        from .search import SimulatorSearch, worker_share
        slots = cfg.concurrent_battles if cfg.mode == "ladder" else 1
        sizing = {"workers": worker_share(slots, cfg.cpu_level)} if slots > 1 or cfg.cpu_level != "high" else {}
        def build():
            try:
                return SimulatorSearch(meta=meta, **sizing)
            except TypeError:  # simplified test doubles take no sizing
                return SimulatorSearch(meta=meta)
        search = self._session_search = build()
        extra = [build() for _ in range(slots - 1)]
        self._extra_searches = extra
        self._search_factory = build
        self._bonus = 0
        self._emit({"type": "status", "message": "Preparing the simulation engine..."})
        # Start every simulator at once; sequential start-up took ~4 s each.
        await asyncio.gather(*(background_call(item.ready) for item in (search, *extra)))
        if cfg.format_id != "gen9randombattle":
            problems = await background_call(search.validate_team, cfg.team, cfg.format_id)
            if problems:
                for item in (search, *extra):
                    item.close()
                raise ValueError("Team not valid for this format:\n" + "\n".join(problems))
        if self._stop.is_set():
            for item in (search, *extra):
                item.close()
            return
        player = self.player = StrategicPlayer(
            engine=DecisionEngine(meta=meta, risk=cfg.risk, allow_tera=cfg.allow_tera),
            search=search, search_pool=extra,
            scout=_make_scout(self.base_dir, experience) if cfg.server == "showdown" and cfg.learning_enabled else None,
            engine_factory=lambda: DecisionEngine(meta=player.engine.meta, risk=cfg.risk, allow_tera=cfg.allow_tera),
            emit=self._emit, account_configuration=AccountConfiguration(cfg.username, cfg.password or None),
            experience=experience, team_id=team_fingerprint(cfg.team, cfg.format_id), rating=known_rating,
            learning_enabled=cfg.learning_enabled, base_dir=self.base_dir,
            archetype=cfg.archetype,
            session_store=self.sessions, session_config=cfg, recovery=self.recovery,
            battle_format=cfg.format_id, team=cfg.team if cfg.format_id != "gen9randombattle" else None,
            # Room capacity for "+ New battle"; the search size per battle still follows `slots`.
            server_configuration=self._server(), max_concurrent_battles=MAX_CONCURRENT if cfg.mode == "ladder" else 1,
            avatar=cfg.avatar or None,
            loop=asyncio.get_running_loop(), start_listening=False,
            log_level=logging.WARNING, save_replays=False,
            start_timer_on_battle_start=True,
        )
        from . import foulplay_brain
        player.foulplay_parallelism = foulplay_brain.default_parallelism(slots)
        player.foulplay_engine = cfg.engine
        player.ladder_lookup = cfg.server == "showdown" and cfg.mode == "ladder"
        bridge = _LogBridge(self._emit, lambda message: setattr(self, "_failure", message))
        player.logger.addHandler(bridge)
        # A loaded PC can need more than poke-env's 10 s for the TLS handshake.
        player.ps_client._open_timeout = 30.0
        listen = asyncio.create_task(player.ps_client.listen())
        meta_job = research_job = None
        async def research_replays():
            try:
                from .replay_research import ReplayResearch
                research = ReplayResearch(self.base_dir)
                cached = research.load(cfg.format_id)
                if cached and cfg.learning_enabled:
                    player.research_priors = cached.get("observed_priors", {})
                    await background_call(player.refresh_preferences)
                if cfg.learning_enabled and cfg.auto_update_meta and cfg.format_id != "gen9randombattle":
                    result = await background_call(research.refresh, cfg.format_id, 1700, 6, 10)
                    player.research_priors = result.get("observed_priors", {})
                    await background_call(player.refresh_preferences)
                    self._emit({"type": "log", "message": f"Public research: {result.get('sample_size', 0)} recent high-Elo replays in {cfg.format_id}. " + " ".join(result.get("warnings", []))})
            except Exception as exc:
                self._emit({"type": "log", "message": f"Replay research unavailable: {exc}; usage stats and revealed data are still used."})
        try:
            attempts = 3
            for attempt in range(1, attempts + 1):
                self._emit({"type": "status", "message": f"Connecting as {cfg.username}..." + (f" (attempt {attempt}/{attempts})" if attempt > 1 else "")})
                deadline = time.monotonic() + 45
                problem = ""
                while not player.ps_client.logged_in.is_set():
                    if self._stop.is_set():
                        return
                    if listen.done() or self._failure:
                        problem = self._failure or "Could not open the WebSocket."
                        break
                    if time.monotonic() > deadline:
                        problem = "Login not confirmed within 45 s."
                        break
                    await asyncio.sleep(0.1)
                if not problem:
                    break
                # Only network failures are retried; a refused login is final.
                network = problem.startswith("Connection closed") or "WebSocket" in problem or "45 s" in problem
                if not network or attempt == attempts:
                    if network:
                        raise ConnectionError("Could not connect to Showdown after 3 attempts. "
                                              "Check the internet connection or try again in a few minutes. (" + problem + ")")
                    raise ConnectionError(problem)
                self._emit({"type": "log", "message": f"Connection failed ({problem}); retrying in {2 * attempt} s."})
                self._failure = None
                abort_socket(player.ps_client)
                listen.cancel()
                await asyncio.gather(listen, return_exceptions=True)
                player.ps_client._shutdown_requested = False
                for _ in range(20 * attempt):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(.1)
                listen = asyncio.create_task(player.ps_client.listen())
            self._emit({"type": "log", "message": f"Connected to {cfg.server}; format {cfg.format_id}."})
            # Background downloads start only after login, so they never compete
            # with the connection handshake.
            if cfg.auto_update_meta and cfg.format_id != "gen9randombattle":
                meta_job = asyncio.create_task(refresh_meta(cutoff))
            research_job = asyncio.create_task(research_replays())
            index = 0
            if self.recovery:
                await self._resume_battle(player, listen)
                index = 1
            if cfg.mode == "ladder" and slots > 1 and not self._stop.is_set():
                await self._ladder_pool(player, listen, slots, index)
            while not self._stop.is_set() and (cfg.autoplay or index < cfg.battles) and not (cfg.mode == "ladder" and slots > 1):
                while self._extra_pending and not self._stop.is_set():
                    await asyncio.sleep(.2)
                suffix = " • autoplay" if cfg.autoplay else f"/{cfg.battles}"
                self._emit({"type": "status", "message": f"Waiting for battle {index + 1}{suffix}..."})
                try:
                    await self._play_one(player, listen)
                except TimeoutError:
                    if not cfg.autoplay or self._stop.is_set():
                        raise
                    self._emit({"type": "status", "message": "No opponent found; searching again..."})
                    # Retry a slow ladder without killing an infinite session;
                    # bounded interruptible backoff avoids hammering the server.
                    for _ in range(25):
                        if self._stop.is_set():
                            break
                        await asyncio.sleep(.2)
                    continue
                index += 1
                next_cutoff = rating_bucket(getattr(player, "current_rating", known_rating))
                if next_cutoff != cutoff and cfg.format_id != "gen9randombattle":
                    cutoff = next_cutoff
                    cached = store.load(cfg.format_id, cutoff)
                    if cached:
                        _apply_meta(player, cached)
                    if cfg.auto_update_meta and (meta_job is None or meta_job.done()):
                        meta_job = asyncio.create_task(refresh_meta(cutoff))
        finally:
            # On normal completion, do not abort the transport before the server
            # acknowledges /leave. STOP bypasses this bounded, cancellable wait.
            departures = tuple(getattr(player, "_room_jobs", {}).values())
            if departures and not self._abort.is_set():
                try:
                    await asyncio.wait(departures, timeout=4.5)
                except asyncio.CancelledError:
                    # STOP can arrive during this final wait; still release the
                    # listeners/room tasks below without waiting for the peer.
                    pass
            player.shutting_down = True
            abort_socket(player.ps_client)
            jobs = {listen, *getattr(player.ps_client, "_active_tasks", ()),
                    *getattr(player, "_room_jobs", {}).values()}
            jobs |= {job for job in (meta_job, research_job) if job is not None}
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            player.logger.removeHandler(bridge)
            handler = getattr(player, "_owned_logger_handler", None)
            if handler:
                player.logger.removeHandler(handler)
                handler.close()
            reports = tuple(getattr(player, "_report_jobs", ()))
            if reports and not self._abort.is_set():
                done, pending = await asyncio.wait(reports, timeout=1.5)
                for job in pending:
                    job.cancel()
            for tag in tuple(getattr(player, "battles", {})):
                player._cleanup_room(tag)
            getattr(player.ps_client, "_battle_locks", {}).clear()

    async def _resume_battle(self, player, listen):
        tag = self.recovery["battle_tag"]
        self._emit({"type": "status", "message": f"Recovering {tag}..."})
        # Joining as the authenticated participant returns the full visible log
        # and the current private |request|; /log is not a recovery endpoint.
        await player.ps_client.send_message(f"/join {tag}")
        began = time.monotonic()
        while not player.recovery_ready.is_set():
            if listen.done():
                raise ConnectionError("Connection closed during recovery.")
            if self._failure:
                raise ConnectionError(self._failure)
            if time.monotonic() - began > 15:
                raise TimeoutError("The room sent no player request. Check the account and whether the battle is still active.")
            await asyncio.sleep(.05)
        if player.recovery_error:
            raise ValueError(player.recovery_error)
        self._emit({"type": "status", "message": "Battle recovered; continuing from the turn the server confirmed."})
        while tag not in player._finished_tags:
            if listen.done():
                raise ConnectionError("The connection to the server was closed.")
            await asyncio.sleep(.1)

    async def _ladder_pool(self, player, listen, slots, launched=0):
        """Keep up to ``slots`` ladder battles running at once.

        Showdown accepts a new /search while the account is already playing,
        so a new search starts whenever a slot is free. Turning autoplay off
        stops new searches and lets the running battles finish.
        """
        cfg = self.config
        active = lambda: [tag for tag, battle in player.battles.items() if not battle.finished]
        while not self._stop.is_set() and (cfg.autoplay or launched < cfg.battles):
            if listen.done():
                raise ConnectionError("The connection to the server was closed.")
            if len(active()) >= slots + self._bonus:
                await asyncio.sleep(.25)
                continue
            known = set(player.battles)
            self._failure = None
            running = len(active())
            self._emit({"type": "status", "message": f"Waiting for battle {launched + 1} • {running}/{slots} in progress..."})
            await player.ps_client.search_ladder_game(cfg.format_id, player.next_team)
            began = time.monotonic()
            while True:
                if self._abort.is_set():
                    return launched
                if listen.done():
                    raise ConnectionError("The connection to the server was closed.")
                if set(player.battles) - known:
                    launched += 1
                    if len(active()) > slots and self._bonus:
                        self._bonus -= 1
                    break
                if self._stop.is_set():
                    await player.ps_client.send_message("/cancelsearch")
                    break
                if self._failure and not active():
                    raise RuntimeError(self._failure)
                if time.monotonic() - began > cfg.matchmaking_timeout:
                    await player.ps_client.send_message("/cancelsearch")
                    if not active() and not cfg.autoplay:
                        raise TimeoutError("Timed out waiting for an opponent. Check the format, team and challenge.")
                    self._emit({"type": "status", "message": "No opponent found; searching again..."})
                    break
                await asyncio.sleep(.2)
            # Let the server register the new room before the next /search.
            for _ in range(5):
                if self._abort.is_set():
                    return launched
                await asyncio.sleep(.2)
        while active() and not self._abort.is_set():
            if listen.done():
                raise ConnectionError("The connection to the server was closed.")
            await asyncio.sleep(.3)
        return launched

    async def _play_one(self, player, listen):
        cfg = self.config
        self._failure = None
        if cfg.mode == "ladder":
            coro = player.ladder(1)
        elif cfg.mode == "challenge":
            coro = player.send_challenges(cfg.opponent, 1)
        else:
            coro = player.accept_challenges(cfg.opponent or None, 1)
        job = asyncio.create_task(coro)
        began = time.monotonic()
        started = False
        try:
            while not job.done():
                if self._abort.is_set():
                    return
                active = any(not battle.finished for battle in player.battles.values())
                started = started or active
                if listen.done():
                    raise ConnectionError("The connection to the server was closed.")
                if self._failure and not active:
                    raise RuntimeError(self._failure)
                timed_out = not started and time.monotonic() - began > cfg.matchmaking_timeout
                if (self._stop.is_set() or timed_out) and not active and not started:
                    await player.ps_client.send_message("/cancelsearch")
                    if cfg.mode == "challenge":
                        await player.ps_client.send_message(f"/cancelchallenge {cfg.opponent}")
                    # Allow already-sent battle initialization to arrive before closing.
                    await asyncio.sleep(0.3)
                    if any(not b.finished for b in player.battles.values()):
                        continue
                    if timed_out:
                        raise TimeoutError("Timed out waiting for an opponent. Check the format, team and challenge.")
                    return
                await asyncio.sleep(0.2)
            await job
        finally:
            if not job.done():
                job.cancel()
            await asyncio.gather(job, return_exceptions=True)
