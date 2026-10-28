"""Recovery, cancellation and room lifetime contracts without public networking."""
import asyncio
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from poke_env.ps_client import AccountConfiguration

from unaware.backend import AppService
from unaware.client import configure_client
from unaware.engine import DecisionEngine
from unaware.gui import WindowApi
from unaware.runner import BotConfig, BotRunner, StrategicPlayer
from unaware.session import SessionStore, background_call
from tests.test_telemetry import real_battle


def make_player(**kwargs):
    player = _player(**kwargs)
    return player


def _player(**kwargs):
    return StrategicPlayer(engine=DecisionEngine(), emit=lambda _: None,
                           account_configuration=AccountConfiguration("Bot", None),
                           battle_format="gen9ou", loop=asyncio.get_running_loop(),
                           start_listening=False, **kwargs)


def test_checkpoint_is_atomic_and_never_serializes_password_or_token(tmp_path):
    store = SessionStore(tmp_path)
    store.save({"battle_tag": "battle-gen9ou-7", "username": "Bot", "team": "private build",
                "request": {"rqid": 9}, "password": "do-not-write", "token": "do-not-write"})
    assert store.load()["request"]["rqid"] == 9
    assert "do-not-write" not in store.path.read_text()
    store.clear("battle-gen9ou-8")
    assert store.load()
    store.clear("battle-gen9ou-7")
    assert store.load() is None


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI")
def test_login_cookie_encrypted_and_scoped_to_account_and_auth_server(tmp_path):
    store = SessionStore(tmp_path)
    store.save_cookies("Bot", "https://play.pokemonshowdown.com/action.php", {"sid": "private-session-token"})
    assert "private-session-token" not in store.secret_path.read_text()
    restored = SessionStore(tmp_path)
    assert restored.cookies("BOT", "https://play.pokemonshowdown.com/action.php") == {"sid": "private-session-token"}
    assert restored.cookies("Other", "https://play.pokemonshowdown.com/action.php") == {}
    assert restored.cookies("Bot", "https://other.example/action.php") == {}


def test_stop_interrupts_active_task_without_server_close_handshake(tmp_path):
    ready = threading.Event()
    runner = BotRunner(BotConfig(username="Bot", format_id="gen9randombattle"), lambda _: None, tmp_path)
    transport = SimpleNamespace(abort=Mock())
    search = SimpleNamespace(abort=Mock(), close=Mock())

    async def session():
        runner._session_search = search
        runner.player = SimpleNamespace(ps_client=SimpleNamespace(websocket=SimpleNamespace(transport=transport), _active_tasks=set()),
                                        n_won_battles=0, n_lost_battles=0, n_tied_battles=0, n_finished_battles=0, fallback_count=0)
        ready.set()
        await asyncio.Event().wait()

    runner._run_session = session
    runner.start()
    assert ready.wait(2)
    began = time.monotonic()
    runner.stop()
    runner._thread.join(1)
    assert not runner.is_running and time.monotonic() - began < .8
    search.abort.assert_called_once()
    transport.abort.assert_called_once()


def test_close_window_does_not_wait_for_finished_battle():
    service = SimpleNamespace(running=True, close=Mock())
    api = WindowApi(service)
    api._window = SimpleNamespace(destroy=Mock())
    assert api.close() == {"waiting_for_battle": False}
    service.close.assert_called_once()
    api._window.destroy.assert_called_once()


def test_recover_without_saved_room_does_not_launch_new_match(tmp_path):
    factory = Mock()
    app = AppService(tmp_path, runner_factory=factory)
    with pytest.raises(ValueError, match="No saved active battle"):
        app.recover()
    factory.assert_not_called()


def test_wait_request_and_already_sent_rqid_never_resubmit():
    async def run():
        p = make_player()
        p.ps_client.send_message = AsyncMock()
        p.choose_move = Mock(return_value=SimpleNamespace(message="/choose move 1"))
        battle = SimpleNamespace(battle_tag="battle-gen9ou-7", finished=False, _wait=True,
                                 last_request={"rqid": 3, "wait": True}, teampreview=False)
        await p._handle_battle_request(battle)
        p.choose_move.assert_not_called()
        battle._wait = False
        battle.last_request = {"rqid": 4}
        await p._handle_battle_request(battle)
        await p._handle_battle_request(battle)
        p.choose_move.assert_called_once()
        p.ps_client.send_message.assert_awaited_once_with("/choose move 1", battle.battle_tag, "4")
    asyncio.run(run())


def test_cancelled_decision_cannot_send_a_late_order():
    async def run():
        p = make_player()
        entered, release = threading.Event(), threading.Event()
        def choose(_):
            entered.set()
            release.wait(2)
            return SimpleNamespace(message="/choose move 1")
        p.choose_move = choose
        p.ps_client.send_message = AsyncMock()
        battle = SimpleNamespace(battle_tag="battle-gen9ou-7", finished=False, _wait=False,
                                 last_request={"rqid": 4}, teampreview=False)
        task = asyncio.create_task(p._handle_battle_request(battle))
        while not entered.is_set():
            await asyncio.sleep(.001)
        p.shutting_down = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release.set()
        await asyncio.sleep(.02)
        p.ps_client.send_message.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", [["", "win", "Bot"], ["", "tie"]])
def test_result_leaves_before_analysis_and_deinit_does_not_resurrect_room(terminal):
    async def run():
        entered, release = threading.Event(), threading.Event()
        def persist(*args, **kwargs):
            entered.set()
            release.wait(2)
            return {"summary": "saved"}
        experience = SimpleNamespace(record_battle=persist, policy_weights=lambda *_: {})
        p = make_player(experience=experience)
        b = real_battle()
        p._battles[b.battle_tag] = b
        p._battle_count_queue.put_nowait(None)
        p.ps_client.send_message = AsyncMock()
        await p._handle_battle_message([[">" + b.battle_tag], terminal])
        p.ps_client.send_message.assert_awaited_once_with("/leave " + b.battle_tag)
        while not entered.is_set():
            await asyncio.sleep(.001)
        await p._handle_battle_message([[">" + b.battle_tag], ["", "deinit"]])
        release.set()
        await asyncio.gather(*p._report_jobs)
        assert b.battle_tag not in p.battles
        assert b.battle_tag not in p.traces
        assert b.battle_tag not in p.completed_records
        assert not p._room_jobs and not p._sent_requests
        assert p.n_finished_battles == 1
        await p._handle_battle_message([[">" + b.battle_tag], terminal])
        assert p.n_finished_battles == 1
    asyncio.run(run())


def test_cancelled_legacy_background_read_does_not_delay_event_loop_shutdown():
    release = threading.Event()
    async def run():
        task = asyncio.create_task(background_call(release.wait, 3))
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    began = time.monotonic()
    try:
        asyncio.run(run())
        assert time.monotonic() - began < .5
    finally:
        release.set()


def test_login_http_request_is_cancellable_without_socket_timeout(tmp_path):
    from aiohttp import web
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        async def login(request):
            entered.set()
            await release.wait()
            return web.Response(text=']{"assertion":"test"}')
        app = web.Application()
        app.router.add_post("/auth", login)
        server = web.AppRunner(app)
        await server.setup()
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = SimpleNamespace(username="Bot", _avatar=None, send_message=AsyncMock(),
                                 account_configuration=SimpleNamespace(password="unused local test"),
                                 server_configuration=SimpleNamespace(authentication_url=f"http://127.0.0.1:{port}/auth"))
        configure_client(client, SessionStore(tmp_path))
        task = asyncio.create_task(client.log_in(["", "challstr", "1", "local-test"]))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            began = time.monotonic()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert time.monotonic() - began < .2
            client.send_message.assert_not_awaited()
        finally:
            release.set()
            await server.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("reply,ok", [("unregistered-assertion", True), (";", False)])
def test_login_without_password_asks_for_name_assertion(tmp_path, reply, ok):
    from aiohttp import web
    async def run():
        seen = {}
        async def login(request):
            seen.update(await request.post())
            return web.Response(text=reply)
        app = web.Application()
        app.router.add_post("/auth", login)
        server = web.AppRunner(app)
        await server.setup()
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        client = SimpleNamespace(username="Guest Name", _avatar=None, send_message=AsyncMock(),
                                 account_configuration=SimpleNamespace(password=None),
                                 server_configuration=SimpleNamespace(authentication_url=f"http://127.0.0.1:{port}/auth"))
        configure_client(client, SessionStore(tmp_path))
        try:
            if ok:
                await client.log_in(["", "challstr", "1", "abc"])
                client.send_message.assert_awaited_once_with("/trn Guest Name,0,unregistered-assertion")
            else:
                with pytest.raises(ValueError, match="registered"):
                    await client.log_in(["", "challstr", "1", "abc"])
                client.send_message.assert_not_awaited()
            assert seen == {"challstr": "1|abc", "act": "getassertion", "userid": "guestname"}
        finally:
            await server.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize("terminal", [["", "win", "Bot"], ["", "tie"]])
def test_closed_socket_during_leave_still_saves_result(terminal):
    from websockets.exceptions import ConnectionClosedError

    async def run():
        saved = Mock(return_value={"summary": "saved"})
        p = make_player(experience=SimpleNamespace(record_battle=saved, policy_weights=lambda *_: {}))
        battle = real_battle()
        p._battles[battle.battle_tag] = battle
        p.ps_client.send_message = AsyncMock(side_effect=ConnectionClosedError(None, None))
        await p._handle_battle_message([[">" + battle.battle_tag], terminal])
        await asyncio.gather(*p._report_jobs)
        assert p.n_finished_battles == 1
        saved.assert_called_once()
        assert saved.call_args.args[0]["result"] == ("win" if terminal[1] == "win" else "tie")
        p._cleanup_room(battle.battle_tag)

    asyncio.run(run())


def test_recovery_of_finished_room_clears_checkpoint_and_never_matches_again(tmp_path):
    async def run():
        sessions = SessionStore(tmp_path)
        battle = real_battle()
        checkpoint = {"battle_tag": battle.battle_tag, "username": "Bot"}
        sessions.save(checkpoint)
        p = make_player(session_store=sessions, recovery=checkpoint)
        p._battles[battle.battle_tag] = battle
        p.ps_client.send_message = AsyncMock()
        await p._handle_battle_message([[">" + battle.battle_tag], ["", "init", "battle"], ["", "win", "Bot"]])
        runner = BotRunner(BotConfig(username="Bot", format_id="gen9randombattle", autoplay=True), lambda _: None, tmp_path)
        runner.recovery = checkpoint
        with pytest.raises(ValueError, match="has already ended"):
            await runner._resume_battle(p, SimpleNamespace(done=lambda: False))
        assert sessions.load() is None
        assert [call.args[0] for call in p.ps_client.send_message.await_args_list] == [
            "/leave " + battle.battle_tag, "/join " + battle.battle_tag]
        await asyncio.gather(*p._report_jobs)
        p._cleanup_room(battle.battle_tag)

    asyncio.run(run())


def test_unacknowledged_leave_retries_and_cleans_up_without_changing_result():
    async def run():
        events = []
        p = make_player()
        p.emit = events.append
        battle = real_battle()
        p._battles[battle.battle_tag] = battle
        p.ps_client.send_message = AsyncMock()
        await p._handle_battle_message([[">" + battle.battle_tag], ["", "win", "Bot"]])
        await asyncio.wait_for(p._room_jobs[battle.battle_tag], 4)
        assert p.ps_client.send_message.await_count == 3
        assert p.n_won_battles == 1
        assert not p.battles and not p._room_jobs and not p._room_departures
        assert any(e.get("type") == "room_closed" and not e["confirmed"] for e in events)
        assert any("did not confirm" in e.get("message", "") for e in events)
    asyncio.run(run())


def test_leave_confirmation_stops_retries_even_after_trace_has_been_released():
    async def run():
        events = []
        p = make_player()
        p.emit = events.append
        battle = real_battle()
        p._battles[battle.battle_tag] = battle
        p.ps_client.send_message = AsyncMock()
        await p._handle_battle_message([[">" + battle.battle_tag], ["", "tie"]])
        job = p._room_jobs[battle.battle_tag]
        p.traces.clear()
        await p._handle_battle_message([[">" + battle.battle_tag], ["", "deinit"]])
        await asyncio.gather(job, return_exceptions=True)
        assert p.ps_client.send_message.await_count == 1
        assert not p.battles and not p._room_departures and not p._room_jobs
        assert any(e.get("type") == "room_closed" and e["confirmed"] for e in events)
    asyncio.run(run())


def test_shutdown_cancels_waiting_leave_without_server_ack():
    async def run():
        p = make_player()
        battle = real_battle()
        p._battles[battle.battle_tag] = battle
        p.ps_client.send_message = AsyncMock()
        await p._handle_battle_message([[">" + battle.battle_tag], ["", "win", "Bot"]])
        job = p._room_jobs[battle.battle_tag]
        await asyncio.sleep(.01)
        began = time.monotonic()
        p.shutting_down = True
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        assert time.monotonic() - began < .1
        assert not p._room_jobs and not p._room_departures
        assert p.ps_client.send_message.await_count == 1
    asyncio.run(run())
