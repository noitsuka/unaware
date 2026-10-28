"""'+ New battle': one more ladder battle while the current one keeps running."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from unaware.runner import BotConfig, BotRunner


def runner_with(tmp_path, battles, mode="ladder", concurrent=1):
    runner = BotRunner(BotConfig(username="Bot", mode=mode, concurrent_battles=concurrent), lambda event: None, tmp_path)
    runner._extra_searches = []
    runner._search_factory = lambda: SimpleNamespace(ready=lambda: None)
    runner.player = SimpleNamespace(battles=battles, _pool=[SimpleNamespace()], strategy_profile={}, next_team=None,
                                    ps_client=SimpleNamespace(search_ladder_game=AsyncMock(), send_message=AsyncMock()))
    return runner


def test_request_is_refused_without_a_running_battle(tmp_path):
    runner = runner_with(tmp_path, {"battle-1": SimpleNamespace(finished=True)})
    runner._loop = asyncio.new_event_loop()
    try:
        with pytest.raises(ValueError, match="searching"):
            runner.request_extra_battle()
        runner.config.mode = "challenge"
        with pytest.raises(ValueError, match="ladder"):
            runner.request_extra_battle()
    finally:
        runner._loop.close()


def test_extra_battle_searches_with_its_own_search_and_waits_for_the_room(tmp_path):
    battles = {"battle-1": SimpleNamespace(finished=False)}
    runner = runner_with(tmp_path, battles)
    events = []
    runner._emit = events.append

    async def scenario():
        async def opponent_found(*_):
            battles["battle-2"] = SimpleNamespace(finished=False)
        runner.player.ps_client.search_ladder_game.side_effect = opponent_found
        runner._extra_pending = True
        await runner._extra_battle()

    asyncio.run(scenario())
    runner.player.ps_client.search_ladder_game.assert_awaited_once()
    assert len(runner.player._pool) == 2 and runner._extra_searches  # not sharing the running battle's search
    assert runner._extra_pending is False
    assert any("Extra battle started" in event.get("message", "") for event in events)


def test_pool_mode_opens_one_more_slot(tmp_path):
    runner = runner_with(tmp_path, {"battle-1": SimpleNamespace(finished=False), "battle-2": SimpleNamespace(finished=False)},
                         concurrent=2)
    runner.player._pool = [SimpleNamespace(), SimpleNamespace()]
    runner._emit = lambda event: None
    runner._extra_pending = True
    asyncio.run(runner._extra_battle())
    assert runner._bonus == 1 and len(runner.player._pool) == 3
    runner.player.ps_client.search_ladder_game.assert_not_awaited()  # the pool loop searches
