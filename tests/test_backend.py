"""Local API contracts using isolated storage and real session threads."""
import copy
import http.client
import json
import threading
import time
from types import SimpleNamespace

import pytest

from unaware.backend import AppService, LocalAppServer


TEAM = """Dragonite @ Heavy-Duty Boots
Ability: Multiscale
Tera Type: Normal
EVs: 252 Atk / 4 SpD / 252 Spe
Adamant Nature
- Dragon Dance
- Extreme Speed
- Earthquake
- Ice Spinner
"""


class FakeRunner:
    """Exercise callbacks across a real thread without a ladder connection."""
    instances = []

    def __init__(self, config, callback, base_dir):
        self.config, self.callback = config, callback
        self.done = threading.Event()
        self.started = threading.Event()
        self.stopped = False
        self._thread = None
        self.player = SimpleNamespace(ps_client=SimpleNamespace(websocket=SimpleNamespace(latency=0.024)))
        type(self).instances.append(self)

    @property
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        def work():
            self.callback({"type": "status", "message": "Waiting for battle 1/1..."})
            self.started.set()
            self.done.wait(3)
            self.callback({"type": "finished"})
        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def stop(self):
        self.stopped = True

    def finish(self):
        self.done.set()
        self._thread.join(2)
        assert not self._thread.is_alive()


@pytest.fixture
def service(tmp_path):
    FakeRunner.instances = []
    app = AppService(tmp_path, runner_factory=FakeRunner)
    yield app
    app.close()
    for runner in FakeRunner.instances:
        if runner.is_running:
            runner.finish()


@pytest.fixture
def api(service):
    static = service.base_dir / "frontend/dist"
    static.mkdir(parents=True)
    (static / "index.html").write_text("<html>Local UI</html>", encoding="utf-8")
    (service.base_dir / "secret.txt").write_text("outside-static-root", encoding="utf-8")
    server = LocalAppServer(service)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, method="GET", body=None, token=False, cookie=False, origin=None, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        headers = {}
        if token:
            headers["X-Showdown-Token"] = server.token
        if cookie:
            headers["Cookie"] = f"showdown-session={server.token}"
        if origin:
            headers["Origin"] = origin
        if host:
            headers["Host"] = host
        data = json.dumps(body) if body is not None else None
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        result = (response.status, text, dict(response.getheaders()))
        connection.close()
        return result
    yield request, server
    server.shutdown()
    server.server_close()
    worker.join(2)


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_local_session_requires_token_and_same_origin(api):
    request, server = api
    status, body, headers = request("/api/session")
    assert status == 200 and json.loads(body)["token"] == server.token
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Strict" in headers["Set-Cookie"]
    assert request("/api/state")[0] == 403
    assert request("/api/state", cookie=True)[0] == 200
    assert request("/api/state", token=True, origin="https://evil.example")[0] == 403
    assert request("/api/session", host="evil.example")[0] == 403
    assert request("/api/config", "POST", {"username": "Bot"}, cookie=True)[0] == 403
    assert request("/api/config", "POST", {"username": "Bot"}, token=True,
                   origin="https://evil.example")[0] == 403
    assert request("/api/config", "POST", {"username": "Bot"}, token=True)[0] == 200


@pytest.mark.parametrize("changes", [
    {"format_id": "unsupported", "username": "Changed"},
    {"username": 42}, {"risk": True}, {"battles": 1.5}, {"allow_tera": "yes"},
    {"matchmaking_timeout": float("nan")}, {"battles": 101},
    {"selected_team": "missing.txt", "username": "Changed"},
])
def test_invalid_settings_never_partially_mutate_or_persist(service, changes):
    service.update_config({"username": "Original"})
    before = copy.deepcopy(service.config)
    saved = (service.base_dir / "config.json").read_bytes()
    with pytest.raises(ValueError):
        service.update_config(changes)
    assert service.config == before
    assert (service.base_dir / "config.json").read_bytes() == saved


def test_password_is_remembered_encrypted_until_logout(service, api):
    request, _ = api
    service.update_config({"username": "Bot", "password": "private-test-value"})
    assert service.sessions.account_password("Bot") == "private-test-value"
    assert service.state()["account_saved"] is True
    assert "password" not in service.state()["config"]
    for path in (service.base_dir / "config.json", *(service.base_dir / "data").glob("*")):
        assert b"private-test-value" not in path.read_bytes()
    assert "private-test-value" not in request("/api/state", token=True)[1]
    service.logout()
    assert service.sessions.account_password("Bot") == "" and service.config.username == ""


@pytest.mark.parametrize("name", ["../escape", "..\\escape", "CON", "NUL.txt", "team:evil", "a/b"])
def test_team_name_rejects_paths_and_windows_devices(service, name):
    with pytest.raises(ValueError):
        service.save_team({"name": name, "text": TEAM})
    assert not list(service.team_dir.glob("*.txt"))


def test_team_save_edit_rename_delete_are_recoverable(service):
    service.save_team({"name": "My Team", "text": TEAM})
    first = (service.team_dir / "My Team.txt").read_text(encoding="utf-8")
    edited = TEAM.replace("Adamant", "Jolly")
    service.save_team({"name": "My Team", "original_name": "My Team.txt", "text": edited})
    archives = list((service.team_dir / ".trash").glob("*.txt"))
    assert len(archives) == 1 and archives[0].read_text(encoding="utf-8") == first
    service.save_team({"name": "Renamed", "original_name": "My Team.txt", "text": edited})
    assert not (service.team_dir / "My Team.txt").exists()
    assert service.selected_team == "Renamed.txt"
    service.delete_team({"name": "Renamed.txt"})
    assert not service.teams() and service.selected_team == ""
    assert len(list((service.team_dir / ".trash").glob("*.txt"))) == 3


def test_unknown_routes_and_encoded_static_traversal_cannot_read_local_files(api):
    request, _ = api
    assert request("/api/unknown", token=True)[0] == 404
    assert request("/api/unknown", "POST", {}, token=True)[0] == 404
    assert request("/")[0] == 200
    for path in ("/../../secret.txt", "/%2e%2e/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        status, text, _ = request(path)
        assert status == 404 and "outside-static-root" not in text


def test_built_sprite_index_takes_precedence_over_legacy_assets(api):
    request, server = api
    built = server.static / "assets"
    legacy = server.service.base_dir / "assets"
    built.mkdir()
    legacy.mkdir()
    (built / "pokemon-icons.json").write_text('{"newform": 1571}')
    (legacy / "pokemon-icons.json").write_text('{"oldform": 520}')
    (legacy / "fallback.txt").write_text("legacy-fallback")
    assert json.loads(request("/assets/pokemon-icons.json")[1]) == {"newform": 1571}
    assert request("/assets/fallback.txt")[1] == "legacy-fallback"


def test_start_stop_real_thread_finishes_current_session_and_locks_edits(service):
    service.start({"username": "Bot", "format_id": "gen9randombattle"})
    runner = service.runner
    assert runner.started.wait(1)
    assert service.state()["running"] and service.state()["status"] == "Waiting"
    with pytest.raises(ValueError):
        service.start({})
    with pytest.raises(ValueError):
        service.update_config({"format_id": "gen9ou"})
    with pytest.raises(ValueError):
        service.save_team({"name": "Blocked", "text": TEAM})
    service.stop()
    assert runner.stopped and service.status == "Stopping" and service.running
    runner.finish()
    assert not service.running and service.status == "Idle"


def test_recover_restarts_after_finished_callback_without_overlapping_workers(service):
    service.start({"username": "Bot", "format_id": "gen9randombattle"})
    first = service.runner
    assert first.started.wait(1)
    service.sessions.save({"battle_tag": "battle-gen9randombattle-42", "username": "Bot",
                           "format_id": "gen9randombattle", "team": "", "server": "showdown"})
    service.recover()
    assert service.recover_pending and service.status == "Recovering"
    assert len(FakeRunner.instances) == 1
    first.finish()
    wait_until(lambda: len(FakeRunner.instances) == 2)
    second = service.runner
    assert second is not first and second.started.wait(1)
    assert second.recovery["battle_tag"] == "battle-gen9randombattle-42"
    assert not first.is_running and service.running
    service.stop()
    second.finish()


def test_elo_uses_latest_known_rating_for_same_account_server_and_tier(service):
    records = [
        ("2026-01-01", "Bot", "showdown", "gen9ou", 1400),
        ("2026-01-02", "bot", "sim3.psim.us", "gen9ou", 1420),
        ("2026-01-03", "Other", "showdown", "gen9ou", 1900),
        ("2026-01-04", "Bot", "localhost:8000", "gen9ou", 1700),
        ("2026-01-05", "Bot", "showdown", "gen9uu", 1300),
        ("2026-01-06", "Bot", "showdown", "gen9ou", None),
    ]
    for index, (date, username, namespace, tier, rating) in enumerate(records):
        service.experience.record_battle({"battle_id": f"api-rating-{index}", "team_id": "test",
            "format_id": tier, "result": "win", "username": username,
            "server_namespace": namespace, "ended_at": date, "rating_after": rating}, [])
    service.handle_event({"type": "rating"})
    service.update_config({"username": "BOT"})
    assert service.state()["ratings"] == {"gen9ou": 1420, "gen9uu": 1300}
    service.update_config({"server": "local"})
    assert service.state()["ratings"] == {"gen9ou": 1700}
    service.update_config({"username": "Other", "server": "showdown"})
    assert service.state()["ratings"] == {"gen9ou": 1900}


def test_performance_does_not_merge_accounts_when_login_is_empty(service):
    service.experience.record_battle({"battle_id": "other-account-win", "team_id": "test",
        "format_id": "gen9ou", "result": "win", "username": "Other",
        "server_namespace": "showdown"}, [])
    assert service.state()["performance"] == {
        "wins": 0, "losses": 0, "ties": 0, "total": 0, "winrate": 0.}
    service.update_config({"username": "other"})
    assert service.state()["performance"]["wins"] == 1
    service.update_config({"username": "Bot"})
    assert service.state()["performance"]["total"] == 0


def test_avatar_is_normalised_validated_and_applied_live(service):
    service.update_config({"avatar": " Cynthia "})
    assert service.config.avatar == "cynthia"
    with pytest.raises(ValueError):
        service.update_config({"avatar": "../evil"})
    assert service.config.avatar == "cynthia"
    calls = []
    service.runner = SimpleNamespace(is_running=True, set_avatar=calls.append)
    service.running = True
    service.update_config({"avatar": "lucas"})
    assert calls == ["lucas"] and service.config.avatar == "lucas"
    service.runner, service.running = None, False




def test_engine_setting_is_validated(service):
    service.update_config({"engine": "foulplay"})
    assert service.config.engine == "foulplay"
    with pytest.raises(ValueError):
        service.update_config({"engine": "chatbot"})


def test_recover_after_a_restart_logs_in_with_the_saved_password(service):
    service.update_config({"username": "Bot", "password": "private-test-value"})
    service.config.password = ""  # a freshly opened app: the password is only in the encrypted store
    service.sessions.save({"battle_tag": "battle-gen9randombattle-43", "username": "Bot",
                           "format_id": "gen9randombattle", "team": "", "server": "showdown"})
    service.recover()
    runner = service.runner
    assert runner.started.wait(1)
    assert runner.config.password == "private-test-value"
    service.stop()
    runner.finish()
