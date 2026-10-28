"""Post-battle evidence must follow identities and actual protocol outcomes."""
import asyncio
import logging
import threading
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
from collections import Counter, OrderedDict

import pytest
from poke_env.battle import Battle, Move, Pokemon, Status
from poke_env.player import Player

from unaware.engine import Candidate
from unaware.runner import StrategicPlayer
from unaware.telemetry import BattleTrace, action_key, context_key, snapshot


def mon(species, hp=1, active=False):
    result = Pokemon(9, species=species)
    result._max_hp = 100
    result._current_hp = round(hp * 100)
    result._active = active
    result.moves["tackle"] = Move("tackle", gen=9)
    return result


def fake_battle():
    own, bench, foe = mon("pikachu", 0.3, True), mon("corviknight"), mon("garchomp", 0.8, True)
    return SimpleNamespace(team={"p1: Pikachu": own, "p1: Corviknight": bench},
                           opponent_team={"p2: Garchomp": foe}, active_pokemon=own,
                           opponent_active_pokemon=foe, turn=3, side_conditions={},
                           opponent_side_conditions={}, fields={}, weather={},
                           force_switch=False, finished=False, battle_tag="battle-gen9ou-1",
                           player_role="p1", opponent_username="Enemy")


def real_battle():
    b = Battle("battle-gen9ou-1", "Bot", logging.getLogger("telemetry-test"), gen=9)
    b._player_role = "p1"
    b._opponent_username = "Enemy"
    own, foe = mon("pikachu", 0.3, True), mon("garchomp", 0.8, True)
    b._team = {"p1: Pikachu": own}
    b._opponent_team = {"p2: Garchomp": foe}
    b._turn = 3
    return b


def start(battle, action=None):
    trace = BattleTrace()
    action = action or battle.active_pokemon.moves["tackle"]
    chosen = Candidate(action, score=8, label="chosen")
    trace.begin(battle, [chosen], chosen, snapshot(battle))
    return trace


def observe_hit(trace, role="p1"):
    trace.observe([["", "move", f"{role}a: Pokemon", "Tackle", "p2a: Garchomp"]], "Bot", role)


def player_stub(battle, directory=None):
    """Construct without starting a network client; tested methods stay real."""
    p = object.__new__(StrategicPlayer)
    p._battles = {battle.battle_tag: battle}
    p._format = "gen9ou"
    p.ps_client = SimpleNamespace(username="Bot", websocket_url="ws://localhost:8000/showdown/websocket",
                                 _battle_locks={}, send_message=AsyncMock())
    p.session_store = None
    p.session_config = None
    p.recovery = None
    p.recovery_ready = asyncio.Event()
    p.recovery_error = ""
    p.shutting_down = False
    p._sent_requests = {}
    p._finished_tags = OrderedDict()
    p._finished_counts = Counter()
    p._room_jobs = {}
    p._room_departures = {}
    p.engine = SimpleNamespace(_last_switch={})
    p._room_state_lock = threading.RLock()
    p._report_jobs = set()
    p._saving_tags = set()
    p._battle_count_queue = asyncio.Queue()
    p._battle_end_condition = asyncio.Condition()
    p.traces = {}
    p.completed_records = {}
    p._learning_cache = {}
    p.session_id = "test-session"
    p.team_id = "team-fingerprint"
    p.learning_enabled = True
    p.base_dir = directory
    p.emit = Mock()
    p.experience = SimpleNamespace(record_battle=Mock(return_value={"summary": "Saved"}), update_rating=Mock(),
                                   policy_weights=Mock(return_value={}),
                                   adjustment=Mock(return_value=(0.0, "")))
    p.fallback_count = 0
    return p


@pytest.mark.parametrize("factory", [fake_battle, real_battle])
def test_snapshot_preserves_identity_hp_and_known_state(factory):
    b = factory()
    state = snapshot(b)
    assert state["active_id"] == "p1: Pikachu"
    assert state["foe_id"] == "p2: Garchomp"
    assert state["active_hp"] == 0.3
    assert state["foe_hp"] == 0.8
    assert state["known_foe_moves"] == ["tackle"]
    assert context_key(state) == context_key(dict(state))


def test_real_snapshot_does_not_invent_preview_hp():
    b = real_battle()
    b._opponent_team = {}
    b._teampreview_opponent_team = [Pokemon(9, species="garchomp")]
    state = snapshot(b)
    assert state["foe_hp_by_id"] == {}
    assert state["foe_alive"] == 0


def test_switch_received_hp_uses_incoming_pokemon_identity():
    b = fake_battle()
    switch = b.team["p1: Corviknight"]
    trace = start(b, switch)
    trace.observe([["", "switch", "p1a: Corviknight", "Corviknight", "100/100"],
                   ["", "-damage", "p1a: Corviknight", "75/100"]], "Bot", "p1")
    switch._current_hp = 75
    b.active_pokemon = switch
    b.turn += 1
    trace.close(b)
    result = trace.decisions[0]["local_outcome"]
    assert result["received"] == pytest.approx(0.25)
    assert result["reliable"] is True
    assert result["own_ko"] is False


def test_opponent_switch_does_not_count_lower_new_active_hp_as_dealt_damage():
    b = fake_battle()
    trace = start(b)
    trace.observe([["", "switch", "p2a: Blissey", "Blissey", "20/100"]], "Bot", "p1")
    replacement = mon("blissey", 0.2, True)
    b.opponent_team["p2: Blissey"] = replacement
    b.opponent_active_pokemon = replacement
    b.turn += 1
    trace.close(b)
    assert trace.decisions[0]["local_outcome"]["dealt"] == 0


def test_forced_switch_observation_is_excluded_from_learning():
    b = fake_battle()
    b.force_switch = True
    trace = start(b, b.team["p1: Corviknight"])
    observe_hit(trace)
    b.turn += 1
    trace.close(b)
    assert trace.decisions[0]["local_outcome"]["reliable"] is False


def test_finished_final_turn_closes_even_without_incremented_turn_number():
    b = real_battle()
    trace = start(b)
    trace.observe([["", "-damage", "p2a: Garchomp", "0 fnt"], ["", "faint", "p2a: Garchomp"]], "Bot", "p1")
    b.opponent_active_pokemon._current_hp = 0
    b.opponent_active_pokemon.status = Status.FNT
    b.won_by("Bot")
    trace.close(b)
    assert trace.decisions[0]["local_outcome"]["reliable"] is True
    assert trace.decisions[0]["local_outcome"]["foe_ko"] is True
    trace.close(b)
    assert len(trace.decisions) == 1


@pytest.mark.parametrize("invalid", [False, True])
def test_same_turn_requests_and_invalid_choice_are_not_training_examples(invalid):
    b = fake_battle()
    trace = start(b)
    observe_hit(trace)
    if invalid:
        trace.observe([["", "error", "[Invalid choice] Can't move"]], "Bot", "p1")
        b.turn += 1
    trace.close(b)
    assert trace.decisions[0]["local_outcome"]["reliable"] is False


def test_protocol_flags_follow_player_role_when_player_is_p2():
    b = fake_battle()
    trace = start(b)
    trace.observe([["", "faint", "p2a: Corviknight"], ["", "-crit", "p2a: Corviknight"],
                   ["", "-damage", "p2a: Corviknight", "10/100", "[from] item: Life Orb"],
                   ["", "-miss", "p1a: Enemy", "p2a: Corviknight"],
                   ["", "-immune", "p1a: Enemy"]], "Bot", "p2")
    flags = trace.pending["observations"]
    assert flags["own_fainted"] is True
    assert not flags.get("foe_fainted", False)
    assert flags["critical"] and flags["miss"] and flags["residual_or_item"] and flags["immune"]


def test_raw_elo_html_and_entities_are_parsed_only_for_this_username():
    trace = BattleTrace()
    trace.observe([["", "player", "p1", "Bot", "1", "1400"]], "Bot", None)
    assert trace.rating_before == 1400
    trace.observe([["", "raw", "<strong>Enemy</strong>'s rating: 1200 &rarr; <strong>1180</strong>"],
                   ["", "raw", "<strong>Bot</strong>'s rating: 1400 &rarr; <strong>1421</strong>"]], "Bot", "p1")
    assert trace.rating_before == 1400 and trace.rating_after == 1421


def test_action_keys_distinguish_moves_tera_and_switches():
    move = Move("tackle", gen=9)
    assert action_key(Candidate(move)) == "move:tackle"
    assert action_key(Candidate(move, terastallize=True)) == "move:tackle:tera"
    assert action_key(Candidate(mon("corviknight"))) == "switch:corviknight"


def test_finished_callback_saves_terminal_replay_and_learns_only_once(tmp_path):
    b = real_battle()
    p = player_stub(b, tmp_path)
    p.traces[b.battle_tag] = start(b)
    observe_hit(p.traces[b.battle_tag])
    b.won_by("Enemy")
    p._battle_finished_callback(b)
    p._battle_finished_callback(b)
    p.experience.record_battle.assert_called_once()
    record, decisions = p.experience.record_battle.call_args.args
    assert record["result"] == "loss" and len(decisions) == 1
    assert decisions[0]["local_outcome"]["reliable"] is True
    assert "|win|Enemy" in __import__("pathlib").Path(record["replay_path"]).read_text(encoding="utf-8")
    assert b.battle_tag in p.completed_records


def test_late_rating_updates_completed_record_without_recording_second_battle(monkeypatch):
    b = real_battle()
    p = player_stub(b)
    b.won_by("Bot")
    p._battle_finished_callback(b)

    async def parent_noop(self, messages):
        pass

    monkeypatch.setattr(Player, "_handle_battle_message", parent_noop)
    batch = [[">" + b.battle_tag], ["", "raw", "Bot's rating: 1300 → 1315"]]
    asyncio.run(p._handle_battle_message(batch))
    asyncio.run(p._handle_battle_message(batch))
    p.experience.record_battle.assert_called_once()
    p.experience.update_rating.assert_called_once()
    assert p.completed_records[b.battle_tag]["rating_after"] == 1315


def test_policy_has_no_learned_value_weights_and_chosen_action_is_traced(monkeypatch):
    b = fake_battle()
    p = player_stub(b)
    first = Candidate(Move("tackle", gen=9), score=10, label="tackle")
    second = Candidate(Move("thunderbolt", gen=9), score=12, label="thunderbolt",
                       diagnostics={"base_score": 9})
    p.engine = SimpleNamespace(rank_actions=Mock(return_value=[first, second]), record_choice=Mock())
    policy = SimpleNamespace(rank=Mock(return_value=[second, first]))
    monkeypatch.setattr("unaware.runner.BattlePolicy", lambda: policy)
    order = p.choose_move(b)
    assert order.order.id == "thunderbolt"
    assert second.diagnostics["base_score"] == 9
    # Learned coefficients no longer change what damage or a KO is worth.
    assert len(policy.rank.call_args.args) == 5
    p.experience.adjustment.assert_not_called()
    assert p.traces[b.battle_tag].pending["action_key"] == "move:thunderbolt"
    p.engine.record_choice.assert_called_once_with(b, second)


def test_disabled_learning_does_not_use_matchup_memory(monkeypatch):
    b = fake_battle()
    p = player_stub(b)
    p.learning_enabled = False
    p._matchup_memory = {("pikachu", "garchomp", "move:tackle"): {"bias": -16, "reason": "x"}}
    candidate = Candidate(Move("tackle", gen=9), score=10, label="tackle")
    p.engine = SimpleNamespace(rank_actions=Mock(return_value=[candidate]), record_choice=Mock())
    policy = SimpleNamespace(rank=Mock(return_value=[candidate]))
    monkeypatch.setattr("unaware.runner.BattlePolicy", lambda: policy)
    assert p.choose_move(b).order.id == "tackle"
    assert candidate.score == 10
    p.experience.adjustment.assert_not_called()


def test_interleaved_request_batch_attributes_later_events_to_new_decision(monkeypatch):
    b = fake_battle()
    p = player_stub(b)
    trace = p.traces[b.battle_tag] = start(b)

    async def parent(self, messages):
        for parts in messages[1:]:
            if parts[1] == "request":
                trace.close(b)
                next_choice = Candidate(Move("thunderbolt", gen=9), label="thunderbolt")
                trace.begin(b, [next_choice], next_choice, snapshot(b))

    monkeypatch.setattr(Player, "_handle_battle_message", parent)
    asyncio.run(p._handle_battle_message([[">" + b.battle_tag], ["", "request", "{}"],
                                          ["", "-immune", "p2a: Garchomp"]]))
    assert not trace.decisions[0]["observations"].get("immune", False)
    assert trace.pending["observations"]["immune"] is True
    assert trace.pending["action_key"] == "move:thunderbolt"


def test_rating_after_win_in_same_batch_updates_persisted_record(monkeypatch):
    b = real_battle()
    p = player_stub(b)

    async def parent(self, messages):
        for parts in messages[1:]:
            if parts[1] == "win":
                b.won_by(parts[2])
                p._battle_finished_callback(b)

    monkeypatch.setattr(Player, "_handle_battle_message", parent)
    async def receive():
        await p._handle_battle_message([[">" + b.battle_tag], ["", "win", "Bot"],
                                      ["", "raw", "Bot's rating: 1510 → 1530"]])
        await asyncio.gather(*p._report_jobs)
        assert p.completed_records[b.battle_tag]["rating_after"] == 1530
    asyncio.run(receive())
    p.experience.record_battle.assert_called_once()


def test_database_failure_keeps_last_matchup_memory_and_legal_choice(monkeypatch):
    b = fake_battle()
    p = player_stub(b)
    first = Candidate(Move("tackle", gen=9), score=10, label="tackle")
    second = Candidate(Move("thunderbolt", gen=9), score=9, label="thunderbolt")
    p.engine = SimpleNamespace(rank_actions=Mock(return_value=[first, second]), record_choice=Mock())
    saved = p._matchup_memory = {}
    p.experience.matchup_memory = Mock(side_effect=OSError("database unavailable"))
    p.refresh_preferences()
    assert p._matchup_memory is saved
    policy = SimpleNamespace(rank=Mock(return_value=[first, second]))
    monkeypatch.setattr("unaware.runner.BattlePolicy", lambda: policy)
    order = p.choose_move(b)
    assert order.order.id == "tackle"
    assert (first.score, second.score) == (10, 9)
    assert p.fallback_count == 0


def test_opponent_switch_is_not_reported_as_new_moves_for_original_foe():
    b = fake_battle()
    trace = start(b)
    replacement = mon("blissey", 0.8, True)
    replacement.moves["softboiled"] = Move("softboiled", gen=9)
    b.opponent_team["p2: Blissey"] = replacement
    b.opponent_active_pokemon = replacement
    b.turn += 1
    observe_hit(trace)
    trace.close(b)
    assert trace.decisions[0]["observations"]["information_revealed"] is False
