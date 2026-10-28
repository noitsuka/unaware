"""Local-only membership regression. Requires the ephemeral LocalRoomAdmin operator.

work/lifecycle-v5-server.cjs starts an official server with this in-memory admin.
Tests real |win| / |tie|, server-side connection membership and a second socket
on the same account. Never connects to a public server or an existing browser.
"""
import asyncio
import json
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from websockets.asyncio.client import connect
from poke_env.player import SimpleHeuristicsPlayer
from poke_env.ps_client import AccountConfiguration
from poke_env.ps_client.server_configuration import LocalhostServerConfiguration
from unaware.runner import StrategicPlayer
from unaware.engine import DecisionEngine
from unaware.client import abort_socket


async def until(predicate, timeout=15):
    began = time.monotonic()
    while not predicate():
        if time.monotonic() - began > timeout:
            raise AssertionError("Local room test timed out")
        await asyncio.sleep(.02)


async def receive(ws, pattern, timeout=5):
    async with asyncio.timeout(timeout):
        while True:
            message = await ws.recv()
            if pattern in message:
                return message


async def login(ws, name):
    await receive(ws, "|challstr|")
    await ws.send(f"|/trn {name},0,")
    await receive(ws, f"|updateuser| {name}|")


async def run():
    events, results = [], []
    nonce = str(time.time_ns())[-7:]
    botname, foename = "Room" + nonce, "Foe" + nonce
    gate = asyncio.Event()

    class HeldPlayer(StrategicPlayer):
        async def _handle_battle_request(self, battle, maybe_default_order=False):
            self.recovery_ready.set()

    class HeldFoe(SimpleHeuristicsPlayer):
        async def choose_move(self, battle):
            await gate.wait()
            return self.choose_default_move()

    common = dict(battle_format="gen9randombattle", server_configuration=LocalhostServerConfiguration,
                  loop=asyncio.get_running_loop(), start_listening=False, log_level=logging.ERROR)
    bot = HeldPlayer(engine=DecisionEngine(), emit=events.append,
                     account_configuration=AccountConfiguration(botname, None), **common)
    foe = HeldFoe(account_configuration=AccountConfiguration(foename, None), **common)
    listeners = [asyncio.create_task(p.ps_client.listen()) for p in (bot, foe)]
    tasks = []
    try:
        await until(lambda: bot.ps_client.logged_in.is_set() and foe.ps_client.logged_in.is_set())
        async with connect("ws://127.0.0.1:8000/showdown/websocket") as admin:
            await login(admin, "LocalRoomAdmin")
            async with connect("ws://127.0.0.1:8000/showdown/websocket") as second:
                await login(second, botname)
                for result in ("win", "tie"):
                    accept = asyncio.create_task(foe.accept_challenges(botname, 1))
                    challenge = asyncio.create_task(bot.send_challenges(foename, 1))
                    tasks.extend((accept, challenge))
                    await until(lambda: bool(bot.battles))
                    tag = next(iter(bot.battles))
                    await second.send(f"|/join {tag}")
                    await receive(second, "|init|battle")
                    await admin.send(f"|/join {tag}")
                    await receive(admin, "|init|battle")
                    command = f"/localtestfinish {botname}" if result == "win" else "/localtestfinish tie"
                    await admin.send(f"{tag}|{command}")
                    try:
                        await until(lambda: any(e.get("type") == "room_closed" and e.get("battle") == tag
                                                and e.get("confirmed") for e in events))
                    except AssertionError:
                        print(json.dumps({"events": events, "room": tag}), flush=True)
                        try:
                            async with asyncio.timeout(.5):
                                while True:
                                    print("admin:", await admin.recv(), flush=True)
                        except TimeoutError:
                            pass
                        raise
                    await asyncio.wait_for(challenge, 3)
                    assert tag not in bot.battles and tag not in bot._room_jobs
                    # Read the actual server connection sets, not Python mocks.
                    await admin.send(f"{tag}|/localtestmembers {botname}")
                    response = await receive(admin, '|raw|[{"joined":')
                    line = next(line for line in response.splitlines() if '|raw|[{"joined":' in line)
                    memberships = json.loads(line.split("|raw|", 1)[1])
                    assert sorted(row["joined"] for row in memberships) == [False, True], response
                    await second.send(f"|/leave {tag}")
                    await receive(second, "|deinit")
                    await admin.send(f"|/leave {tag}")
                    await receive(admin, "|deinit")
                    results.append({"result": result, "bot_departure_confirmed": True,
                                    "bot_room_resources_released": True,
                                    "second_socket_stays_until_it_leaves": True})
        assert bot.n_won_battles == 1 and bot.n_tied_battles == 1
        print(json.dumps({"server": "localhost:8000", "cases": results}), flush=True)
    finally:
        gate.set()
        for player in (bot, foe):
            player.shutting_down = True
            abort_socket(player.ps_client)
        for task in [*listeners, *tasks]:
            task.cancel()
        await asyncio.gather(*listeners, *tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(run())
