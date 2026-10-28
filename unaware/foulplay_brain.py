"""Foul Play as the decision engine (3.13).

Foul Play (github.com/pmariglia/foul-play, GPL-3.0, vendored in vendor/foulplay)
is the strongest open-source Showdown bot: Monte Carlo tree search in the Rust
poke-engine (~300k iterations per sampled state per 1.5 s, vs ~5k nodes in our
JS search), set prediction from Smogon stats plus sets scraped from real
replays, and an evaluation that reached 80% GXE / top-100 in gen9 OU.

The app keeps its own connection, GUI, teams and history. For every battle room
a FoulPlayBattle receives the raw websocket messages, in order, exactly as Foul
Play's own client would, runs Foul Play's battle loop on a private thread and
event loop (its dataset downloads and searches never block the app), and hands
back the command Foul Play wants to send. Any failure or timeout returns None:
the caller then decides with its own search.
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path

VENDOR = Path(__file__).resolve().parents[1] / "vendor" / "foulplay"
_CONFIGURED = False
_LOCK = threading.Lock()
# Each battle thread knows its own account name. Foul Play reads the global
# FoulPlayConfig.username when a battle starts; two accounts in one process
# (local benchmarks) overwrote each other and one battle waited forever.
_LOCAL = threading.local()


async def _battle_tag_and_opponent(ps_websocket_client):
    """fp.modes.base.get_battle_tag_and_opponent, reading this thread's account."""
    from fp.config import FoulPlayConfig
    while True:
        msg = await ps_websocket_client.receive_message()
        split_msg = msg.split("|")
        if "battle" in split_msg[0]:
            battle_tag = split_msg[0].replace(">", "").strip()
            user_name = getattr(_LOCAL, "username", None) or FoulPlayConfig.username
            opponent_name = split_msg[4].replace(user_name, "").replace("vs.", "").strip()
            return battle_tag, opponent_name


_battle_tag_and_opponent.bridge = True
logger = logging.getLogger(__name__)


def available() -> bool:
    try:
        import poke_engine  # noqa: F401
    except Exception:
        return False
    return (VENDOR / "fp").is_dir()


def configure(username: str, pokemon_format: str, search_time_ms: int, parallelism: int) -> None:
    """Foul Play reads one global config; every battle of this app shares it."""
    global _CONFIGURED
    with _LOCK:
        if str(VENDOR) not in sys.path:
            sys.path.insert(0, str(VENDOR))
        from fp.config import FoulPlayConfig, SaveReplay
        FoulPlayConfig.username = username
        FoulPlayConfig.password = None
        FoulPlayConfig.user_id = "".join(c for c in username.lower() if c.isalnum())
        FoulPlayConfig.avatar = None
        FoulPlayConfig.pokemon_format = pokemon_format
        FoulPlayConfig.smogon_stats = None
        FoulPlayConfig.search_time_ms = int(search_time_ms)
        # Also the size of each decision's process pool: never below what a
        # battle in this process asked for (its own sample count is per battle).
        floor = FoulPlayConfig.parallelism if _CONFIGURED else 1
        FoulPlayConfig.parallelism = max(1, floor, int(parallelism))
        FoulPlayConfig.team_preview_search_time_ms = int(search_time_ms)
        FoulPlayConfig.team_preview_search_parallelism = max(1, int(parallelism))
        FoulPlayConfig.search_threads = 1
        FoulPlayConfig.save_replay = SaveReplay.never
        FoulPlayConfig.log_to_file = False
        FoulPlayConfig.file_log_handler = None
        _patch_battle_tag()
        _patch_battle_mode()
        _patch_search_params()
        _patch_scouting_prior()
        _patch_move_selection()
        _configure_logging()
        _CONFIGURED = True


def _patch_battle_tag() -> None:
    # Import once here: never import Foul Play concurrently from battle threads.
    import fp.modes.base as fp_base
    import fp.run_battle  # noqa: F401
    import fp.teams.team_converter  # noqa: F401
    if not getattr(fp_base.get_battle_tag_and_opponent, "bridge", False):
        fp_base.get_battle_tag_and_opponent = _battle_tag_and_opponent


def _patch_battle_mode() -> None:
    # Foul Play keeps ONE mode object (with its Smogon/team set datasets) per
    # battle type, for a bot that plays one battle at a time. Simultaneous
    # battles re-initialised the shared datasets mid-search ("'list' object
    # has no attribute 'get'", or a battle that never decided). Every battle
    # now gets its own mode instance.
    import fp.run_battle as fp_run
    from fp.modes import BATTLE_MODES
    if not getattr(fp_run.battle_mode, "bridge", False):
        def fresh_mode(battle_type):
            mode = type(BATTLE_MODES[battle_type])()
            # Created on the battle's own thread: carries that battle's search size.
            mode.bridge_search = getattr(_LOCAL, "search", None)
            # Shared with the bridge, filled when scouting arrives (see
            # prepare_battles below; the mode survives deepcopy as itself).
            mode.bridge_scouting = getattr(_LOCAL, "scouting", None)
            # Filled after each decision with the network's p(win) (neural.winprob).
            mode.bridge_winprob = getattr(_LOCAL, "winprob", None)
            # "hybrid": the network breaks Foul Play's near-ties (neural.decide).
            mode.bridge_engine = getattr(_LOCAL, "engine", "foulplay")
            mode.bridge_loops = getattr(_LOCAL, "loops", True)
            mode.bridge_endgame = getattr(_LOCAL, "endgame", True)
            mode.bridge_worlds = getattr(_LOCAL, "worlds", 0)
            mode.bridge_guards = getattr(_LOCAL, "guards", True)
            mode.bridge_pools = getattr(_LOCAL, "pools", None)
            return mode
        fresh_mode.bridge = True
        fp_run.battle_mode = fresh_mode


def _patch_search_params() -> None:
    # Search size per battle (3.14): the global config is shared by every
    # battle in the process, so two bots in one benchmark overwrote each
    # other's settings. A battle's own (parallelism, ms) scales Foul Play's
    # sample count and sets its time per sample.
    from fp.config import FoulPlayConfig
    from fp.modes.standard_battle import StandardBattleMode as StandardBattle
    if not getattr(StandardBattle.search_params, "bridge", False):
        original = StandardBattle.search_params

        def search_params(self, battle):
            samples, ms = original(self, battle)
            override = getattr(self, "bridge_search", None)
            if override:
                parallelism, ms = override
                samples = max(1, samples * parallelism // max(1, FoulPlayConfig.parallelism))
            worlds = getattr(self, "bridge_worlds", 0)
            if worlds > samples:
                # More sampled worlds for the same thinking time (WORLDS): each
                # world gets a shorter search, the opponent's possible sets are
                # covered 4x more finely. Without a battle clock the fixed time
                # per world shrinks by the extra rounds, so a turn takes as long.
                lanes = max(1, FoulPlayConfig.parallelism)
                ms = max(200, ms * -(-samples // lanes) // -(-worlds // lanes))
                samples = worlds
            factor = endgame_factor(battle) if getattr(self, "bridge_endgame", True) else 1.0
            return samples, clock_ms(battle, samples, ms, factor)
        search_params.bridge = True
        StandardBattle.search_params = search_params


def _patch_scouting_prior() -> None:
    # Opponent scouting as a prior on the sets Foul Play samples (3.14,
    # see fp_scouting). The mode is NOT copied with the battle (its
    # __deepcopy__ returns itself), so the prior is applied once per
    # (scouting, team) and re-applied from the saved original if it changes.
    from fp.modes.standard_battle import StandardBattleMode as StandardBattle
    if not getattr(StandardBattle.prepare_battles, "bridge", False):
        original_prepare = StandardBattle.prepare_battles

        def prepare_battles(self, battle, num_battles):
            holder = getattr(self, "bridge_scouting", None)
            if holder and holder.get("games"):
                try:
                    from .fp_scouting import apply_prior, scouted_sets
                    names = [p.name for p in [battle.opponent.active, *battle.opponent.reserve] if p is not None]
                    key = (len(holder["games"]), tuple(sorted(names)))
                    if getattr(self, "bridge_prior_key", None) != key:
                        data = self.team_datasets
                        base = getattr(self, "bridge_prior_base", None)
                        if base is None:
                            base = self.bridge_prior_base = (
                                {k: list(v) for k, v in data.pkmn_sets.items()},
                                {k: list(v) for k, v in data.raw_pkmn_moves.items()})
                        data.pkmn_sets = {k: list(v) for k, v in base[0].items()}
                        data.raw_pkmn_moves = {k: list(v) for k, v in base[1].items()}
                        self.bridge_prior_key = key
                        prior = scouted_sets(holder["games"], names)
                        if prior:
                            applied = apply_prior(data, prior)
                            logger.debug("scouting prior %s: %s", battle.battle_tag, applied)
                except Exception:
                    logger.warning("scouting prior failed", exc_info=True)
            return original_prepare(self, battle, num_battles)
        prepare_battles.bridge = True
        StandardBattle.prepare_battles = prepare_battles


def _patch_move_selection() -> None:
    # Moves certain to do nothing (immune target, status on a statused
    # target, hazard already maxed) leave the final choice when anything
    # else is close (3.15, see fp_noop). The battle reaches the selection
    # through a thread-local: Foul Play runs the whole search on one thread.
    import fp.modes.base as fp_base
    import fp.search.main as fp_search
    if not getattr(fp_search.select_move_from_mcts_results, "bridge", False):
        import random
        from .fp_noop import filter_policy
        original_find = fp_search.find_best_move
        original_select = fp_search.select_move_from_mcts_results

        def find_best_move(battle):
            _LOCAL.searching = battle
            try:
                return original_find(battle)
            finally:
                _LOCAL.searching = None
                _estimate_win(battle)

        def select_move_from_mcts_results(mcts_results):
            battle = getattr(_LOCAL, "searching", None)
            if battle is None:
                return original_select(mcts_results)
            policy, score, visits = {}, {}, {}
            for result, chance, _ in mcts_results:
                for option in result.side_one:
                    policy[option.move_choice] = policy.get(option.move_choice, 0) + (
                        chance * option.visits / result.total_visits)
                    score[option.move_choice] = score.get(option.move_choice, 0) + chance * option.total_score
                    visits[option.move_choice] = visits.get(option.move_choice, 0) + chance * option.visits
            # The search's own value estimate per move (mean score), next to its visit share.
            values = {c: score[c] / visits[c] for c in visits if visits[c] > 0}
            ranked = sorted(policy.items(), key=lambda x: x[1], reverse=True)
            foe = getattr(battle.opponent, "active", None)
            logger.debug("policy %s vs %s %s: %s", battle.battle_tag, getattr(foe, "name", None),
                         {k: getattr(foe, k, None) for k in ("types", "status", "terastallized", "tera_type", "hp", "ability", "item")},
                         [(c, round(w, 3)) for c, w in ranked[:6]])
            try:
                kept = filter_policy(ranked, battle)
            except Exception:
                logger.warning("no-op filter failed", exc_info=True)
                kept = ranked
            if len(kept) != len(ranked):
                logger.info("no-op moves dropped %s: %s", battle.battle_tag,
                            [c for c, _ in ranked if c not in dict(kept)])
            if getattr(battle.mode, "bridge_guards", True):
                # Destiny Bond trades and choice locks into a resist (fp_guards).
                try:
                    from .fp_guards import apply as guard
                    guarded = guard(kept, battle)
                    if len(guarded) != len(kept):
                        logger.info("traps avoided %s: %s", battle.battle_tag,
                                    [c for c, _ in kept if c not in dict(guarded)])
                    kept = guarded
                except Exception:
                    logger.warning("move guards failed", exc_info=True)
            # Foul Play's own rule on what is left: any option within 75% of the best.
            best = kept[0][1]
            close = [p for p in kept if p[1] >= best * .75]
            hybrid = getattr(battle.mode, "bridge_engine", "foulplay") == "hybrid"
            preference = None

            def by_network(options):
                nonlocal preference
                if preference is None:
                    from .neural.decide import net_policy
                    preference = net_policy(battle)
                known = [c for c in options if c in preference]
                return max(known, key=preference.get) if known else None
            pick = None
            if len(close) > 1 and hybrid:
                # Hybrid engine: Foul Play draws among near-ties; the network picks instead.
                pick = by_network([c for c, _ in close])
            if pick is None:
                pick = random.choices(close, weights=[p[1] for p in close])[0][0]
            # Battle memory: leave a loop without progress (see fp_loops).
            from .fp_loops import alternatives, is_loop, record
            memory = battle.mode.__dict__.setdefault("bridge_memory", [])
            try:
                if getattr(battle.mode, "bridge_loops", True) and is_loop(memory, battle, pick):
                    from .fp_noop import is_noop
                    options = [c for c in alternatives(pick, dict(kept), values) if not is_noop(c, battle)]
                    if options:
                        escape = (by_network(options) if hybrid else None) or max(options, key=values.get)
                        logger.info("loop without progress %s: %s instead of %s", battle.battle_tag, escape, pick)
                        pick = escape
            except Exception:
                logger.warning("battle memory failed", exc_info=True)
            record(memory, battle, pick)
            return pick
        find_best_move.bridge = True
        select_move_from_mcts_results.bridge = True
        fp_search.ProcessPoolExecutor = _BattlePool
        fp_search.find_best_move = find_best_move
        fp_search.select_move_from_mcts_results = select_move_from_mcts_results
        fp_base.find_best_move = find_best_move


def _configure_logging() -> None:
    fp_logger = logging.getLogger("fp")
    fp_logger.setLevel(logging.WARNING)
    debug = os.environ.get("SHOWDOWN_FP_LOG")
    if debug and not any(isinstance(h, logging.FileHandler) for h in fp_logger.handlers):
        handler = logging.FileHandler(debug, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(threadName)s %(name)s %(message)s"))
        fp_logger.addHandler(handler)
        fp_logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)


class _BattlePool:
    """Foul Play's search opens a new ProcessPoolExecutor on every decision. On
    Windows each worker is a fresh Python that re-imports Foul Play and its
    data: measured 2.7 s a decision with the CPU busy, against 0.1 s for a pool
    kept open. One pool per battle (held by FoulPlayBattle, shut when the battle
    ends); without a battle (tools) it behaves like before."""

    def __init__(self, max_workers):
        from concurrent.futures import ProcessPoolExecutor
        battle = getattr(_LOCAL, "searching", None)
        holder = getattr(getattr(battle, "mode", None), "bridge_pools", None)
        pool = holder.get("pool") if holder is not None else None
        if pool is None or getattr(pool, "_broken", False) or pool._max_workers != max_workers:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            pool = ProcessPoolExecutor(max_workers=max_workers)
            if holder is not None:
                holder["pool"] = pool
        self.pool, self.owned = pool, holder is None

    def __enter__(self):
        return self.pool

    def __exit__(self, *exc):
        if self.owned:
            self.pool.shutdown()
        return False


def _estimate_win(battle, worlds: int = 16) -> None:
    """Network p(win) over Foul Play's sampled worlds, stored for the launcher."""
    holder = getattr(getattr(battle, "mode", None), "bridge_winprob", None)
    if holder is None:
        return
    try:
        from copy import deepcopy

        from fp.search.poke_engine_helpers import battle_to_poke_engine_state

        from .neural.winprob import win_probability
        world = deepcopy(battle)
        if world.team_preview:
            world.user.active = world.user.reserve.pop(0)
            world.opponent.active = world.opponent.reserve.pop(0)
        samples = world.mode.prepare_battles(world, worlds)
        value = win_probability([battle_to_poke_engine_state(b).to_string() for b, _ in samples])
        if value is not None:
            holder["value"], holder["turn"] = value, battle.turn
    except Exception:
        logger.debug("win estimate failed", exc_info=True)


RESERVE_S, MIN_TURN_S, MAX_TURN_S = 45, 2, 25
WORLDS = 32  # sampled opponent worlds per decision (Foul Play's default: 8, 16 early on)


def clock_ms(battle, samples: int, ms: int, factor: float = 1.0) -> int:
    """Search time per sampled world from the battle clock (3.19).

    Showdown's ladder timer gives a 150 s bank and +10 s a turn. A turn may use
    (bank - 45 s) / 3, times the endgame factor, between 2 and 25 s, split over
    the rounds of parallel worlds: long thinking while the bank is full, then
    about 10-12 s a turn with ~75 s always in reserve. Without a timer the fixed
    time (times the endgame factor) is kept."""
    remaining = getattr(battle, "time_remaining", None)
    if not remaining:
        return int(ms * factor)
    from fp.config import FoulPlayConfig
    rounds = max(1, -(-samples // max(1, FoulPlayConfig.parallelism)))
    turn = min(MAX_TURN_S, max(MIN_TURN_S, (remaining - RESERVE_S) / 3 * factor))
    return int(turn * 1000 / rounds)


def endgame_factor(battle) -> float:
    """More search time as the battle shrinks (3.19): with few Pokémon left each
    choice decides more and the tree is smaller, so a deeper search pays off.
    x1.5 with 6 or fewer alive on both sides together, x2 with 4 or fewer; the
    ladder clock (150 s bank, +10 s a turn) covers it."""
    try:
        alive = sum(1 for side in (battle.user, battle.opponent)
                    for mon in [side.active, *side.reserve] if mon is not None and mon.hp > 0)
        unseen = max(0, 6 - 1 - len(battle.opponent.reserve)) if battle.opponent.active else 0
    except Exception:
        return 1.0
    alive += unseen  # opposing Pokémon not revealed yet are alive
    return 2.0 if alive <= 4 else 1.5 if alive <= 6 else 1.0


class _BridgeClient:
    """The subset of Foul Play's PSWebsocketClient its battle loop uses."""

    def __init__(self, owner: "FoulPlayBattle"):
        self.owner = owner
        self.username = owner.username

    async def receive_message(self) -> str:
        return await self.owner.inbox.get()

    async def send_message(self, room, message_list):
        first = str(message_list[0]) if message_list else ""
        logger.debug("FP -> %s %s", room, message_list)
        if first.startswith(("/choose", "/switch", "/team")):
            rqid = message_list[1] if len(message_list) > 1 else ""
            if first.startswith("/team") and "|" in first:
                first, rqid = first.split("|", 1)
            self.owner.decisions.put((str(rqid), first))
        # Foul Play's own "hf", "/timer on" and "gg" are dropped: the bot does not chat.

    async def leave_battle(self, battle_tag):
        return None

    async def save_replay(self, battle_tag):
        return None


class FoulPlayBattle:
    def __init__(self, battle_tag: str, username: str, pokemon_format: str, team_text: str,
                 search_ms: int | None = None, parallelism: int | None = None,
                 engine: str = "foulplay", loops: bool = True, endgame: bool = True, worlds: int = 0,
                 guards: bool = True):
        self.battle_tag = battle_tag
        self.search = (max(1, int(parallelism)), int(search_ms)) if search_ms and parallelism else None
        self.scouting: dict = {"games": []}
        self.engine = engine if engine in ("foulplay", "hybrid") else "foulplay"
        self.loops = bool(loops)
        self.endgame = bool(endgame)
        self.worlds = max(0, int(worlds or 0))
        self.guards = bool(guards)
        self.pools: dict = {}  # this battle's search processes (_BattlePool)
        self.winprob: dict = {"value": None, "turn": None}
        self.username = username
        self.pokemon_format = pokemon_format
        self.team_text = team_text
        self.decisions: queue.Queue = queue.Queue()
        self.error: str = ""
        self.finished = False
        self.loop = asyncio.new_event_loop()
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.thread = threading.Thread(target=self._run, name=f"foulplay-{battle_tag}", daemon=True)
        self.thread.start()

    def feed(self, raw: str) -> None:
        if not self.finished:
            logger.debug("feed %s: %s", self.battle_tag, raw[:160].replace(chr(10), " / "))
            self.loop.call_soon_threadsafe(self.inbox.put_nowait, raw)

    def set_scouting(self, games: list) -> None:
        """This opponent's scouted games (cache/scouting); used from the next search on."""
        self.scouting["games"] = list(games or [])

    def decision(self, rqid, timeout: float) -> str | None:
        """Foul Play's command for this request id, or None (timeout/failure)."""
        deadline = time.monotonic() + timeout
        wanted = "" if rqid is None else str(rqid)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (self.finished and self.decisions.empty()):
                return None
            try:
                got, message = self.decisions.get(timeout=min(.25, remaining))
            except queue.Empty:
                continue
            if not wanted or got == wanted:
                return message
            # An older request's answer that arrived late: discard it.

    def close(self) -> None:
        self.finished = True
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass

    def _run(self) -> None:
        _LOCAL.username = self.username
        _LOCAL.search = self.search
        _LOCAL.scouting = self.scouting
        _LOCAL.engine = self.engine
        _LOCAL.loops = self.loops
        _LOCAL.endgame = self.endgame
        _LOCAL.worlds = self.worlds
        _LOCAL.guards = self.guards
        _LOCAL.pools = self.pools
        _LOCAL.winprob = self.winprob
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._battle())
        except Exception as exc:  # the caller falls back to its own search
            self.error = f"{type(exc).__name__}: {exc}"
            logger.warning("Foul Play failed in %s: %s", self.battle_tag, self.error, exc_info=True)
        finally:
            self.finished = True
            pool = self.pools.pop("pool", None)
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)

    async def _battle(self) -> None:
        from fp.run_battle import pokemon_battle
        from fp.teams.team_converter import export_to_dict
        team_dict = export_to_dict(self.team_text.replace("\r", "").strip() + "\n")
        await pokemon_battle(_BridgeClient(self), self.pokemon_format, team_dict)


def default_parallelism(concurrent_battles: int) -> int:
    cores = os.cpu_count() or 4
    return max(1, min(8, (cores - 2) // max(1, concurrent_battles)))
