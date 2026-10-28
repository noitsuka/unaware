import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from unaware.runner import BotConfig, BotRunner


class RunnerConfigurationTests(unittest.TestCase):
    def test_public_settings_never_include_password(self):
        config = BotConfig(username="LocalBot", password="private password")
        self.assertNotIn("password", config.public_dict())
        self.assertNotIn("private password", json.dumps(config.public_dict()))

    def test_remote_plaintext_authentication_with_password_rejected(self):
        config = BotConfig(username="LocalBot", password="private", format_id="gen9randombattle",
                           server="custom", websocket_url="wss://example.com/ws",
                           authentication_url="http://example.com/action.php")
        self.assertTrue(any("HTTPS" in error for error in config.validate()))
        config.authentication_url = "https://example.com/action.php"
        self.assertEqual(config.validate(), [])

    def test_invalid_parameters_fail_before_connecting(self):
        config = BotConfig(username="bot|injection", format_id="gen9randombattle", battles=0, risk=2)
        errors = config.validate()
        self.assertTrue(any("name" in error for error in errors))
        self.assertTrue(any("battles" in error for error in errors))
        self.assertTrue(any("Risk" in error for error in errors))

    def test_redaction_preserves_json_and_event_shape(self):
        for password in ("n", 'quote"slash\\', "secret"):
            with self.subTest(password=password), tempfile.TemporaryDirectory() as directory:
                events = []
                config = BotConfig(username="LocalBot", password=password)
                runner = BotRunner(config, events.append, Path(directory))
                runner._log_path = Path(directory) / "session.jsonl"
                runner._emit({"type": "log", "message": f"first line\npassword: {password}",
                              "nested": {"reasons": [f"echo {password}"]}})
                self.assertEqual(events[0]["type"], "log")
                self.assertIn("[redacted]", events[0]["message"])
                self.assertIn("[redacted]", events[0]["nested"]["reasons"][0])
                saved = json.loads(runner._log_path.read_text(encoding="utf-8"))
                self.assertEqual(saved, events[0])


class RunnerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = BotRunner(BotConfig(username="LocalBot", format_id="gen9randombattle"),
                                lambda event: None, Path(self.directory.name))
        self.release = asyncio.Event()
        self.job_cancelled = asyncio.Event()

        async def play(*args):
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.job_cancelled.set()
                raise

        self.player = SimpleNamespace(
            battles={}, ps_client=SimpleNamespace(send_message=AsyncMock()),
            ladder=AsyncMock(side_effect=play), send_challenges=AsyncMock(side_effect=play),
            accept_challenges=AsyncMock(side_effect=play),
        )
        self.listen = asyncio.get_running_loop().create_future()

    async def test_stop_while_searching_cancels_search_and_pending_job(self):
        self.runner._stop.set()
        await self.runner._play_one(self.player, self.listen)
        self.player.ps_client.send_message.assert_awaited_once_with("/cancelsearch")
        self.assertTrue(self.job_cancelled.is_set())

    async def test_stop_while_challenging_cancels_outstanding_challenge(self):
        self.runner.config.mode = "challenge"
        self.runner.config.opponent = "OtherBot"
        self.runner._stop.set()
        await self.runner._play_one(self.player, self.listen)
        self.player.send_challenges.assert_awaited_once_with("OtherBot", 1)
        commands = [call.args[0] for call in self.player.ps_client.send_message.await_args_list]
        self.assertIn("/cancelchallenge OtherBot", commands)

    async def test_stop_allows_active_battle_to_finish(self):
        battle = SimpleNamespace(finished=False)
        self.player.battles["battle-local"] = battle
        self.runner._stop.set()

        async def finish_battle():
            await asyncio.sleep(0.03)
            battle.finished = True
            self.release.set()

        finish = asyncio.create_task(finish_battle())
        await self.runner._play_one(self.player, self.listen)
        await finish
        self.player.ps_client.send_message.assert_not_awaited()
        self.assertFalse(self.job_cancelled.is_set())

    async def test_connection_loss_is_reported(self):
        self.listen.set_result(None)
        with self.assertRaisesRegex(ConnectionError, "connection"):
            await self.runner._play_one(self.player, self.listen)

    async def test_wait_timeout_cancels_search_and_raises(self):
        # Call the internal loop with an already-expired limit to avoid clock
        # mocking that would also modify asyncio's own monotonic clock.
        self.runner.config.matchmaking_timeout = -1
        with self.assertRaisesRegex(TimeoutError, "Timed out"):
            await self.runner._play_one(self.player, self.listen)
        self.player.ps_client.send_message.assert_awaited_once_with("/cancelsearch")

    async def test_accept_mode_routes_unrestricted_or_named_opponent(self):
        self.runner.config.mode = "accept"
        self.runner._stop.set()
        await self.runner._play_one(self.player, self.listen)
        self.player.accept_challenges.assert_awaited_once_with(None, 1)


if __name__ == "__main__":
    unittest.main()
