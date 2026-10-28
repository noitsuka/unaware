"""Real local-only reconnect test; requires Showdown at localhost:8000.

Creates isolated throwaway accounts/storage, pauses the opponent, disconnects
the bot after it sent a choice, and resumes that exact request without sending
the already accepted choice twice. No public server is contacted.
"""
import asyncio
import json
import logging
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from poke_env.player import SimpleHeuristicsPlayer
from poke_env.ps_client import AccountConfiguration
from poke_env.ps_client.server_configuration import LocalhostServerConfiguration
from unaware.runner import BotConfig, BotRunner
from unaware.client import abort_socket


async def until(predicate, timeout=20):
    began = time.monotonic()
    while not predicate():
        if time.monotonic() - began > timeout:
            raise AssertionError("Local recovery test timed out")
        await asyncio.sleep(.02)


async def run():
    gate = asyncio.Event()

    class HeldOpponent(SimpleHeuristicsPlayer):
        async def choose_move(self, battle):
            await gate.wait()
            return super().choose_move(battle)

    nonce = str(time.time_ns())[-7:]
    foe = HeldOpponent(account_configuration=AccountConfiguration("Hold" + nonce, None),
                       server_configuration=LocalhostServerConfiguration,
                       battle_format="gen9randombattle", loop=asyncio.get_running_loop(),
                       start_listening=False, log_level=logging.ERROR)
    listener = asyncio.create_task(foe.ps_client.listen())
    accept = asyncio.create_task(foe.accept_challenges(None, 1))
    runners = []
    with tempfile.TemporaryDirectory(prefix="showdown-recovery-local-") as directory:
        base = Path(directory)
        events = []
        cfg = BotConfig(username="Resume" + nonce, format_id="gen9randombattle", server="local",
                        mode="challenge", opponent=foe.username, auto_update_meta=False, learning_enabled=False)
        first = BotRunner(cfg, events.append, base)
        runners.append(first)
        try:
            await until(lambda: foe.ps_client.logged_in.is_set())
            first.start()
            await until(lambda: (first.sessions.load() or {}).get("sent_rqid") is not None, 45)
            checkpoint = first.sessions.load()
            tag, old_rqid = checkpoint["battle_tag"], checkpoint["sent_rqid"]
            began = time.monotonic()
            first.stop()
            await until(lambda: not first.is_running, 2)
            stop_ms = round((time.monotonic() - began) * 1000)
            assert stop_ms < 1000, stop_ms
            recovered_events = []
            recovered = BotRunner(cfg, recovered_events.append, base)
            recovered.recovery = recovered.sessions.load()
            runners.append(recovered)
            recovered.start()
            await until(lambda: any(e.get("message", "").startswith("Batalha recuperada") for e in recovered_events), 45)
            assert not any(e.get("type") == "error" for e in recovered_events), recovered_events
            assert list(recovered.player.battles) == [tag]
            assert recovered.player.battles[tag].last_request["rqid"] == old_rqid
            assert not any(e.get("type") == "decision" for e in recovered_events), "Repeated an accepted choice"
            gate.set()
            await until(lambda: any(e.get("type") == "decision" for e in recovered_events), 20)
            assert recovered.player.battles[tag].last_request["rqid"] > old_rqid
            began = time.monotonic()
            recovered.stop()
            await until(lambda: not recovered.is_running, 2)
            second_stop_ms = round((time.monotonic() - began) * 1000)
            assert second_stop_ms < 1000, second_stop_ms
            print(json.dumps({"recovered_same_room": True, "confirmed_rqid": old_rqid,
                              "duplicate_choices": 0, "continued_next_turn": True,
                              "stop_ms": [stop_ms, second_stop_ms]}), flush=True)
        finally:
            for runner in runners:
                runner.stop()
            gate.set()
            for tag in tuple(foe.battles):
                try:
                    await foe.ps_client.send_message("/forfeit", tag)
                except Exception:
                    pass
            await asyncio.sleep(.15)
            abort_socket(foe.ps_client)
            accept.cancel()
            listener.cancel()
            await asyncio.gather(accept, listener, return_exceptions=True)
            await until(lambda: all(not r.is_running for r in runners), 3)


if __name__ == "__main__":
    asyncio.run(run())
