"""Bridge between the app and the vendored Foul Play decision engine."""
from types import SimpleNamespace

from poke_env.battle import Move

from unaware import foulplay_brain
from unaware.engine import Candidate
from unaware.runner import StrategicPlayer
from test_decision_regressions import pokemon


class FakeBridge:
    def __init__(self, answer=None):
        self.fed, self.closed, self.answer, self.error = [], False, answer, ""

    def feed(self, raw):
        self.fed.append(raw)

    def close(self):
        self.closed = True

    def decision(self, rqid, timeout):
        return self.answer


def host(bridge, tag="battle-gen9ou-1"):
    events = []
    return SimpleNamespace(brain_mode="foulplay", _fp_battles={tag: bridge}, _team_text="x",
                           emit=events.append, events=events,
                           _foulplay_wait=lambda battle, seconds: StrategicPlayer._foulplay_wait(host_ref[0], battle, seconds))


host_ref = [None]


def test_tier_line_does_not_end_the_bridge():
    """'|tier|[Gen 9] OU' contains '|tie': the bridge shut down on turn 0."""
    bridge = FakeBridge()
    me = host(bridge)
    StrategicPlayer._observe_raw(me, ">battle-gen9ou-1\n|gen|9\n|tier|[Gen 9] OU\n|rule|Sleep Clause")
    assert bridge.fed and not bridge.closed
    StrategicPlayer._observe_raw(me, ">battle-gen9ou-1\n|\n|win|someone")
    # The result line is delivered; Foul Play's own loop ends on it (no forced stop).
    assert bridge.fed[-1].endswith("|win|someone") and not bridge.closed
    assert "battle-gen9ou-1" not in me._fp_battles


def test_other_rooms_and_global_messages_are_ignored():
    bridge = FakeBridge()
    me = host(bridge)
    StrategicPlayer._observe_raw(me, "|updateuser| Bot|1|1|")
    StrategicPlayer._observe_raw(me, ">battle-gen9ou-2\n|move|p1a: X|Tackle")
    assert bridge.fed == []


def battle_with_request(team_details):
    return SimpleNamespace(battle_tag="battle-gen9ou-1", turn=5,
                           last_request={"rqid": 7, "side": {"pokemon": [{"details": d} for d in team_details]}})


def test_switch_and_move_commands_map_to_candidates():
    gholdengo = pokemon("gholdengo", ("makeitrain", "shadowball"), [0, 0, 0, 252, 4, 252], "timid", "goodasgold", "choicescarf")
    move = Candidate(Move("closecombat", gen=9), score=0, label="closecombat")
    tera = Candidate(Move("closecombat", gen=9), score=0, label="closecombat + Tera")
    tera.terastallize = True
    switch = Candidate(gholdengo, score=0, label="Switch: gholdengo")
    candidates = [move, tera, switch]
    for answer, expected in (("/switch 2", switch), ("/choose move closecombat", move),
                             ("/choose move closecombat terastallize", tera)):
        me = host(FakeBridge(answer))
        host_ref[0] = me
        state = battle_with_request(["Zamazenta", "Gholdengo"])
        assert StrategicPlayer._foulplay_choice(me, state, candidates, 2000) is expected


def test_unknown_command_falls_back_to_our_search():
    me = host(FakeBridge("/choose move hyperbeam"))
    host_ref[0] = me
    move = Candidate(Move("closecombat", gen=9), score=0, label="closecombat")
    assert StrategicPlayer._foulplay_choice(me, battle_with_request(["Zamazenta"]), [move], 2000) is None
    assert any("has no match" in event.get("message", "") for event in me.events)


def test_engine_is_installed():
    """The Rust engine (built with --features poke-engine/terastallization) is importable."""
    assert foulplay_brain.available()
    import poke_engine
    assert hasattr(poke_engine, "monte_carlo_tree_search")


def test_each_battle_gets_its_own_foul_play_datasets():
    """Foul Play's single shared mode object was re-initialised by a second
    simultaneous battle mid-search ('list' object has no attribute 'get')."""
    foulplay_brain.configure("bridgetest", "gen9ou", 100, 1)
    import fp.run_battle as fp_run
    from fp.constants import BattleType
    first, second = fp_run.battle_mode(BattleType.STANDARD_BATTLE), fp_run.battle_mode(BattleType.STANDARD_BATTLE)
    assert first is not second and first.smogon_sets is not second.smogon_sets


def test_search_size_is_per_battle():
    """3.14: two bots in one process (benchmarks) overwrote each other's global
    search settings. Each battle's mode carries its own (parallelism, ms)."""
    import threading
    foulplay_brain.configure("bridgetest", "gen9ou", 2000, 4)
    import fp.run_battle as fp_run
    from fp.constants import BattleType
    modes = {}

    def make(name, search):
        foulplay_brain._LOCAL.search = search
        modes[name] = fp_run.battle_mode(BattleType.STANDARD_BATTLE)

    for name, search in (("big", (8, 3000)), ("small", (4, 2000)), ("default", None)):
        thread = threading.Thread(target=make, args=(name, search))
        thread.start()
        thread.join()
    foe = SimpleNamespace(hp=100, moves=[1, 2, 3, 4])
    # No timer: the per-battle settings alone (the clock is tested separately).
    battle = SimpleNamespace(team_preview=False, time_remaining=None, opponent=SimpleNamespace(active=foe))
    assert modes["big"].search_params(battle) == (8, 3000)
    assert modes["small"].search_params(battle) == (4, 2000)
    from fp.config import FoulPlayConfig
    assert modes["default"].search_params(battle) == (FoulPlayConfig.parallelism, FoulPlayConfig.search_time_ms)
    # Unknown moves on the opponent's active: Foul Play doubles the samples.
    foe.moves = []
    assert modes["big"].search_params(battle) == (16, 3000)


def test_unparseable_line_does_not_stop_foul_play():
    """Revival Blessing on an Illusion Zoroark Foul Play never saw ('p2: Zoroark')
    crashed its battle loop on 15/10; every later turn fell back to our search."""
    foulplay_brain.configure("Bot", "gen9ou", 1000, 1)
    from fp.battle.protocol import process_battle_updates
    from fp.battle.state import Battle, Pokemon
    battle = Battle("battle-gen9ou-1")
    battle.user.name, battle.opponent.name = "p1", "p2"
    battle.user.active = Pokemon("gliscor", 100)
    battle.opponent.active = Pokemon("pawmot", 100)
    battle.msg_list = ["|-heal|p2: Zoroark|50/100|[from] move: Revival Blessing", "|turn|7"]
    process_battle_updates(battle)
    assert battle.turn == 7


def test_search_time_follows_the_battle_clock():
    from types import SimpleNamespace
    foulplay_brain.configure("Bot", "gen9ou", 1000, 8)
    full = foulplay_brain.clock_ms(SimpleNamespace(time_remaining=150), 16, 3000)
    low = foulplay_brain.clock_ms(SimpleNamespace(time_remaining=60), 16, 3000)
    steady = foulplay_brain.clock_ms(SimpleNamespace(time_remaining=80), 8, 3000)
    assert full == 12500 and low == 2500  # 25 s / 2 rounds; 5 s / 2 rounds
    assert 11000 <= steady <= 12000  # one round, ~11.7 s with 80 s left
    assert foulplay_brain.clock_ms(SimpleNamespace(time_remaining=None), 8, 3000, 1.5) == 4500


def test_search_processes_are_kept_for_the_whole_battle():
    from types import SimpleNamespace

    from unaware import foulplay_brain
    holder = {}
    foulplay_brain._LOCAL.searching = SimpleNamespace(mode=SimpleNamespace(bridge_pools=holder))
    try:
        with foulplay_brain._BattlePool(1) as first:
            pass
        with foulplay_brain._BattlePool(1) as second:
            pass
        assert first is second is holder["pool"]  # not shut down between decisions
    finally:
        foulplay_brain._LOCAL.searching = None
        holder["pool"].shutdown()
