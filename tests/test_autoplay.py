"""Autoplay has no game cap and its switch can stop an active session."""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from unaware.backend import AppService
from unaware.runner import BotConfig, BotRunner
import unaware.runner as runner_module
import unaware.search as search_module


@pytest.mark.parametrize("autoplay,expected", [(False, 1), (True, 103)])
def test_autoplay_runs_past_legacy_cap_until_disabled(monkeypatch, tmp_path, autoplay, expected):
    class Store:
        def __init__(self, *args, **kwargs): pass
        def load(self, *args): return None
    class Search:
        def __init__(self, **kwargs): self.closed = False
        def ready(self): pass
        def close(self): self.closed = True
    class Player:
        def __init__(self, **kwargs):
            self.logger = logging.getLogger("autoplay-test")
            self.ps_client = SimpleNamespace(logged_in=asyncio.Event(), websocket=SimpleNamespace(close=AsyncMock()))
            self.ps_client.logged_in.set()
            async def listen(): await asyncio.Event().wait()
            self.ps_client.listen = listen
    monkeypatch.setattr(runner_module, "MetaStore", Store)
    monkeypatch.setattr(search_module, "SimulatorSearch", Search)
    monkeypatch.setattr(runner_module, "StrategicPlayer", Player)
    runner = BotRunner(BotConfig(username="Bot", format_id="gen9randombattle", auto_update_meta=False,
                                 autoplay=autoplay, battles=1), lambda _: None, tmp_path)
    played = []
    async def play(*args):
        played.append(len(played) + 1)
        if len(played) == 103: runner.set_autoplay(False)
    monkeypatch.setattr(runner, "_play_one", play)
    asyncio.run(runner._run())
    assert len(played) == expected
    assert runner._session_search.closed


def test_disabling_live_autoplay_is_saved_and_requests_graceful_stop(tmp_path):
    app = AppService(tmp_path)
    runner = BotRunner(BotConfig(autoplay=True), lambda _: None, tmp_path)
    # Give the service a live runner identity without starting networking.
    runner._thread = SimpleNamespace(is_alive=lambda: True)
    app.running, app.runner, app.config.autoplay = True, runner, True
    app.set_autoplay({"enabled": False})
    assert runner._stop.is_set() and not runner.config.autoplay
    assert not app.config.autoplay and app.status == "Stopping"
    assert json.loads((tmp_path / "config.json").read_text())["autoplay"] is False


def test_cannot_reenable_a_session_already_stopping(tmp_path):
    runner = BotRunner(BotConfig(), lambda _: None, tmp_path)
    runner.stop()
    with pytest.raises(ValueError, match="finishing"):
        runner.set_autoplay(True)


@pytest.mark.parametrize("value", ["false", 0, None])
def test_autoplay_requires_boolean_not_truthy_text(tmp_path, value):
    app = AppService(tmp_path)
    with pytest.raises(ValueError, match="Autoplay"):
        app.set_autoplay({"enabled": value})
