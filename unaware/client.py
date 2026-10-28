"""Cancellable login and bounded connection shutdown for poke-env."""
from __future__ import annotations

import asyncio
import json
from types import MethodType

import aiohttp
from yarl import URL
from websockets.asyncio.client import connect
from poke_env.ps_client import PSClient


def abort_socket(client):
    """Cut transport locally: never wait for a peer's WebSocket close reply."""
    client._shutdown_requested = True
    websocket = getattr(client, "websocket", None)
    transport = getattr(websocket, "transport", None)
    if transport:
        transport.abort()
    for task in tuple(getattr(client, "_active_tasks", ())):
        if task is not asyncio.current_task():
            task.cancel()


def configure_client(client, sessions):
    """Keep poke-env parsing, replacing only blocking requests-based login."""
    async def log_in(self, message):
        auth_url = self.server_configuration.authentication_url
        password = self.account_configuration.password
        cookies = sessions.cookies(self.username, auth_url)
        if (password or cookies) and URL(auth_url).scheme != "https" and URL(auth_url).host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Remote authentication requires HTTPS.")
        # Every login needs an assertion from the login server, unregistered names
        # included: those get one for the name alone (no password, no cookie).
        userid = "".join(c for c in self.username.lower() if c.isalnum())
        jar = aiohttp.CookieJar(unsafe=URL(auth_url).host in {"localhost", "127.0.0.1", "::1"})
        if cookies:
            jar.update_cookies(cookies, response_url=URL(auth_url))
        async with aiohttp.ClientSession(cookie_jar=jar, timeout=aiohttp.ClientTimeout(total=10)) as http:
            data = {"challstr": message[2] + "|" + message[3]}
            if password:
                data.update(act="login", name=self.username, **{"pass": password})
            else:
                data.update(act="getassertion", userid=userid)
            async with http.post(auth_url, data=data, allow_redirects=False) as response:
                if response.status != 200:
                    raise ConnectionError("Authentication server unavailable.")
                text = await response.text()
            if password:
                try:
                    result = json.loads(text[1:] if text.startswith("]") else text)
                    assertion = result.get("assertion", "") if isinstance(result, dict) else ""
                except (ValueError, AttributeError):
                    assertion = ""
            else:
                assertion = text.strip()
            if not assertion or assertion.startswith(";") or "\n" in assertion or "<" in assertion:
                if password:
                    raise ValueError("Login not confirmed: wrong password for this account.")
                raise ValueError("This name is registered on Showdown. Enter its password in Settings > Account.")
            tokens = {key: morsel.value for key, morsel in jar.filter_cookies(URL(auth_url)).items()}
            if tokens and (password or cookies):
                sessions.save_cookies(self.username, auth_url, tokens)
        await self.send_message(f"/trn {self.username},0,{assertion}")
        if self._avatar is not None:
            await self.change_avatar(self._avatar)

    client._shutdown_requested = False

    async def listen(self):
        def completed(task):
            self._active_tasks.discard(task)
            if not task.cancelled():
                error = task.exception()
                if error and not self._shutdown_requested:
                    self.logger.error("Failed to handle a room event: %s: %s", type(error).__name__, error)

        try:
            async with connect(self.websocket_url, max_queue=32, open_timeout=self._open_timeout,
                               ping_interval=self._ping_interval, ping_timeout=self._ping_timeout,
                               close_timeout=.2) as websocket:
                self.websocket = websocket
                async for message in websocket:
                    if self._shutdown_requested:
                        break
                    task = asyncio.create_task(self._handle_message(str(message)))
                    self._active_tasks.add(task)
                    task.add_done_callback(completed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._shutdown_requested:
                self.logger.error("Connection closed: %s", type(exc).__name__)

    # Showdown queues at most ~6 messages per connection, released every 600 ms,
    # and silently drops the rest ("typing too quickly"). With 5 battles ending,
    # leaving and searching together, a dropped /choose meant a turn with no move.
    original_send = client.send_message
    pacing = {"next": 0.0}

    async def send_message(self, message, room="", message_2=None):
        loop = asyncio.get_running_loop()
        now = loop.time()
        slot = max(now, pacing["next"])
        pacing["next"] = slot + THROTTLE_DELAY
        delay = slot - now - THROTTLE_BURST * THROTTLE_DELAY
        if delay > 0:
            await asyncio.sleep(delay)
        await original_send(message, room, message_2)

    client.log_in = MethodType(log_in, client)
    client.listen = MethodType(listen, client)
    if isinstance(client, PSClient):
        client.send_message = MethodType(send_message, client)


THROTTLE_DELAY = .6
THROTTLE_BURST = 3
