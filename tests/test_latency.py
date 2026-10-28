"""Startup scheduling and worker ownership without external network requests."""
import asyncio
import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from unaware.meta import MetaSnapshot
from unaware.runner import BotConfig, BotRunner
import unaware.runner as runner_module
import unaware.search as search_module


def meta(month="2026-08"):
    return MetaSnapshot("gen9ou", month, 1695, "https://www.smogon.com/stats/test", {},
                        downloaded_at="2026-10-01T00:00:00+00:00")


class FakeMetaStore:
    def __init__(self, path, timeout=20):
        self.timeout = timeout

    def load(self, tier, rating=None):
        return meta()

    def update(self, *args):
        raise AssertionError("Unexpected network refresh")


@pytest.mark.parametrize("phase", ["ready", "validation_rpc", "invalid_team", "player_init"])
def test_search_worker_closes_when_startup_fails_before_websocket(monkeypatch, tmp_path, phase):
    instances = []
    class Search:
        def __init__(self, **kwargs):
            self.closed = False
            instances.append(self)
        def ready(self):
            if phase == "ready":
                raise RuntimeError("Worker startup failed")
        def validate_team(self, *args):
            if phase == "validation_rpc":
                raise RuntimeError("Validation RPC failed")
            return ["Illegal team"] if phase == "invalid_team" else []
        def close(self):
            self.closed = True
    def player(**kwargs):
        raise RuntimeError("Player setup failed")
    monkeypatch.setattr(runner_module, "MetaStore", FakeMetaStore)
    monkeypatch.setattr(search_module, "SimulatorSearch", Search)
    monkeypatch.setattr(runner_module, "StrategicPlayer", player)
    runner = BotRunner(BotConfig(username="Bot", auto_update_meta=False), lambda _: None, tmp_path)
    with pytest.raises((RuntimeError, ValueError)):
        asyncio.run(runner._run())
    assert len(instances) == 1 and instances[0].closed
    assert runner.player is None


def test_stop_before_warmup_never_spawns_node_or_connects(monkeypatch, tmp_path):
    def unexpected(**kwargs):
        raise AssertionError("Search spawned after stop was requested")
    monkeypatch.setattr(runner_module, "MetaStore", FakeMetaStore)
    monkeypatch.setattr(search_module, "SimulatorSearch", unexpected)
    runner = BotRunner(BotConfig(username="Bot", auto_update_meta=False), lambda _: None, tmp_path)
    runner._stop.set()
    asyncio.run(runner._run())
    assert runner._session_search is None and runner.player is None


def test_slow_meta_refresh_cannot_delay_cached_matchmaking(monkeypatch, tmp_path):
    update_started, release_update, update_finished = threading.Event(), threading.Event(), threading.Event()
    calls, matches = [], []
    fresh = meta("2026-09")
    class Store(FakeMetaStore):
        def __init__(self, path, timeout=20):
            super().__init__(path, timeout)
            calls.append(("timeout", timeout))
        def update(self, tier, rating, budget):
            calls.append(("update", tier, rating, budget))
            update_started.set()
            assert release_update.wait(3), "Test did not release background download"
            update_finished.set()
            return fresh
    class Search:
        def __init__(self, meta):
            self.meta, self.closed = meta, False
        def ready(self):
            pass
        def validate_team(self, *args):
            return []
        def close(self):
            self.closed = True
    class Player:
        def __init__(self, engine, search, **kwargs):
            self.engine, self.search = engine, search
            self.logger = logging.getLogger("latency-test-player")
            self.ps_client = SimpleNamespace(logged_in=asyncio.Event(), websocket=SimpleNamespace(close=AsyncMock(), transport=SimpleNamespace(abort=Mock())))
            self.ps_client.logged_in.set()
            async def listen():
                await asyncio.Event().wait()
            self.ps_client.listen = listen
    monkeypatch.setattr(runner_module, "MetaStore", Store)
    monkeypatch.setattr(search_module, "SimulatorSearch", Search)
    monkeypatch.setattr(runner_module, "StrategicPlayer", Player)
    runner = BotRunner(BotConfig(username="Bot"), lambda _: None, tmp_path)
    async def play(player, listen):
        await asyncio.wait_for(asyncio.to_thread(update_started.wait), timeout=1)
        assert not update_finished.is_set()
        matches.append(time.monotonic())
        assert player.engine.meta.month == "2026-08" and player.search.meta.month == "2026-08"
        release_update.set()
        for _ in range(100):
            if player.search.meta is fresh:
                break
            await asyncio.sleep(0.005)
        assert player.engine.meta is fresh and player.search.meta is fresh
    monkeypatch.setattr(runner, "_play_one", play)
    began = time.monotonic()
    try:
        asyncio.run(runner._run())
    finally:
        release_update.set()
    assert matches and matches[0] - began < 1
    assert ("timeout", 3.0) in calls and ("update", "gen9ou", 0, 8.0) in calls
    assert runner._session_search.closed
    runner.player.ps_client.websocket.transport.abort.assert_called_once()
    runner.player.ps_client.websocket.close.assert_not_awaited()


def test_meta_fetch_bounds_each_request_by_remaining_total_budget(monkeypatch, tmp_path):
    import unaware.meta as module
    now, observed = [100.0], []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            return b"published"
    def urlopen(request, timeout):
        observed.append(timeout)
        return Response()
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    store = module.MetaStore(tmp_path, timeout=3)
    store._deadline = 108.0
    assert store._fetch("https://www.smogon.com/stats/") == "published"
    now[0] = 107.25
    store._fetch("https://www.smogon.com/stats/")
    now[0] = 108.0
    with pytest.raises(module.MetaUpdateError):
        store._fetch("https://www.smogon.com/stats/")
    assert observed == [3, 0.75]
