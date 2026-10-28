"""Real persistent worker threads: coverage, bounded search and immediate STOP."""
import os
import threading
import time

import pytest

from unaware.search import SearchUnavailable, SimulatorSearch
from unaware.policy import BattlePolicy
from unaware.engine import Candidate
from tests.test_engine import pokemon, battle, engine_for


def position():
    own = pokemon("mew", ["psychic", "icebeam", "nastyplot", "recover"], hp=.7, ability="synchronize")
    foe = pokemon("garchomp", ["earthquake", "dragonclaw", "swordsdance", "stealthrock"], ability="roughskin")
    reserve = pokemon("corviknight", ["bravebird", "roost", "uturn", "irondefense"], ability="pressure")
    other = pokemon("blissey", ["seismictoss", "softboiled", "thunderwave", "icebeam"], ability="naturalcure")
    return battle(own, foe, [reserve], [other], can_tera=True, opponent_can_tera=True), engine_for(foe)


@pytest.mark.skipif((os.cpu_count() or 1) < 4, reason="Needs multiple CPU cores")
def test_real_pool_uses_multiple_executors_and_compares_every_action():
    state, engine = position()
    with SimulatorSearch(workers=4, time_budget_ms=4000) as worker:
        assert worker.ready()["workers"] == 4
        process = worker._process
        candidates = engine.rank_actions(state)
        result = worker.rank(state, candidates, engine)
        diagnostic = worker.last_diagnostics
        assert len(result) == len(candidates) == diagnostic["root_actions"]
        assert diagnostic["workers_used"] >= 2 and len(diagnostic["worker_thread_ids"]) >= 2
        assert diagnostic["nodes"] > 0 and diagnostic["scenarios"] > 0
        assert diagnostic["elapsed_ms"] < 5500
        assert all(c.diagnostics["search_legal"] == 1 for c in result)
        assert all(0 <= c.diagnostics["faint_risk"] <= 1 for c in result)
        assert worker.healthcheck()["workers"] == 4 and worker._process is process


@pytest.mark.skipif((os.cpu_count() or 1) < 4, reason="Needs multiple CPU cores")
def test_abort_kills_all_busy_threads_without_waiting_for_search_deadline():
    state, engine = position()
    worker = SimulatorSearch(workers=4, time_budget_ms=15000)
    worker.ready()
    process, failures = worker._process, []
    def run():
        try:
            worker.rank(state, engine.rank_actions(state), engine)
        except SearchUnavailable as error:
            failures.append(str(error))
    task = threading.Thread(target=run)
    try:
        task.start()
        time.sleep(.15)
        began = time.monotonic()
        worker.abort()
        task.join(2)
        assert time.monotonic() - began < 2
        assert not task.is_alive() and failures
        process.wait(timeout=2)
        with pytest.raises(SearchUnavailable, match="cancelled"):
            worker.healthcheck()
    finally:
        worker.close()


def test_lethal_reply_cannot_be_hidden_by_low_damage_positional_reply():
    state, engine = position()
    c = Candidate(state.active_pokemon.moves["psychic"], diagnostics={
        "expected_received": .02, "faint_risk": .01,
        "search_received_worst": .9, "search_faint_risk_worst": 1.})
    vector, _ = BattlePolicy().features(state, c, engine, {"archetype": "balance"}, {})
    assert vector["incoming"] >= .45 and vector["faint_risk"] >= .5
    aggressive, _ = BattlePolicy().features(state, c, engine, {"archetype": "hyper_offense"}, {})
    assert .3 < aggressive["faint_risk"] < vector["faint_risk"]
