"""Regressions found by reviewing the integrated learning and session paths."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from unaware.backend import AppService
from unaware.meta import MetaSnapshot
from unaware.replay_research import parse_public_log
from unaware.runner import BotConfig, BotRunner, StrategicPlayer
from unaware.state import hydrate_public_event
from tests.test_backend import TEAM


def test_disabled_match_learning_cannot_reactivate_cached_research_priors():
    player = StrategicPlayer.__new__(StrategicPlayer)
    player.search = SimpleNamespace(preferences={})
    player.learning_enabled = False
    player.experience = None
    player.research_priors = {"species": {"skarmory": {"confidence": .15, "battles": 8,
                                                       "moves": {"whirlwind": 1.}}}}
    player.refresh_preferences()
    assert player.search.preferences == {}


def test_invalid_rename_source_fails_before_creating_new_export(tmp_path):
    service = AppService(tmp_path)
    try:
        with pytest.raises(ValueError):
            service.save_team({"name": "Uncommitted", "original_name": "../outside.txt", "text": TEAM,
                               "format_id": "gen9ou", "archetype": "balance"})
        assert not (service.team_dir / "Uncommitted.txt").exists()
        assert service.team_metadata == {}
        assert service.selected_team == ""
    finally:
        service.close()


def test_mew_and_mewtwo_are_different_species_not_preview_formes():
    rows = ["|poke|p1|Mew|", "|poke|p1|Mewtwo|", "|poke|p1|Kyogre|", "|poke|p1|Groudon|",
            "|poke|p1|Rayquaza|", "|poke|p1|Arceus|", "|poke|p2|Mimikyu|"]
    rows += ["|switch|p1a: Psychic|Mewtwo, L100|100/100", "|switch|p2a: Fairy|Mimikyu, L100|100/100",
             "|move|p1a: Psychic|Psystrike|p2a: Fairy", "|turn|1", "|win|Player"]
    parsed = parse_public_log("\n".join(rows))
    assert set(parsed["teams"][0]["species"]) == {"mew", "mewtwo", "kyogre", "groudon", "rayquaza", "arceus"}
    assert parsed["teams"][0]["preview_complete"]


@pytest.mark.parametrize("base,forme,ability,event", [
    ("Mimikyu", "Mimikyu-Busted", "disguise", "detailschange"),
    ("Eiscue", "Eiscue-Noice", "iceface", "-formechange"),
])
def test_revealed_broken_protection_forme_is_retained_with_status_and_hp(base, forme, ability, event):
    """Opponent forms lack own requests; rebuilding cannot restore their shield."""
    from poke_env.battle import Pokemon, Status
    from poke_env.data import to_id_str
    mon = Pokemon(gen=9, species=base)
    mon._ability = ability
    mon._status = Status.BRN
    mon._current_hp, mon._max_hp = 82, 100
    mon.forme_change(forme)
    assert mon.species == to_id_str(base)  # behavior of this poke-env version
    battle = SimpleNamespace(gen=9, team={}, _opponent_team={"p2: Fairy": mon})
    hydrate_public_event(battle, ["", event, "p2a: Fairy", f"{forme}, L100"])
    assert mon.species == to_id_str(forme)
    assert mon.ability == ability and mon.status is Status.BRN
    assert (mon.current_hp, mon.max_hp) == (82, 100)


def test_elo_crossing_uses_cached_population_even_with_downloads_disabled(monkeypatch, tmp_path):
    """Offline settings forbid downloads, not an available appropriate cache."""
    import unaware.learning as learning_module
    import unaware.runner as runner_module
    import unaware.search as search_module
    loaded, played = [], []
    class Store:
        def __init__(self, *args, **kwargs):
            pass
        def load(self, format_id, rating=None):
            loaded.append(rating)
            return MetaSnapshot(format_id, "2026-09", rating, f"https://www.smogon.com/stats/2026-09/chaos/{format_id}-{rating}.json", {"mimikyu": {}})
        def update(self, *args):
            pytest.fail("network refresh occurred while automatic downloads were disabled")
    class Experience:
        def __init__(self, *args):
            pass
        def history(self, *args):
            return [{"format_id": "gen9ou", "username": "Bot", "server_namespace": "sim3.psim.us",
                     "rating_after": 1774}]
    class Search:
        def __init__(self, meta):
            self.meta = meta
        def ready(self):
            pass
        def validate_team(self, *args):
            return []
        def close(self):
            pass
    class Player:
        def __init__(self, engine, search, rating, **kwargs):
            self.engine, self.search, self.current_rating = engine, search, rating
            self.research_priors = {}
            self.logger = logging.getLogger("integration-elo-audit")
            self.ps_client = SimpleNamespace(logged_in=asyncio.Event(), websocket=SimpleNamespace(close=AsyncMock()))
            self.ps_client.logged_in.set()
            async def listen():
                await asyncio.Event().wait()
            self.ps_client.listen = listen
        def refresh_preferences(self):
            pass
    monkeypatch.setattr(runner_module, "MetaStore", Store)
    monkeypatch.setattr(learning_module, "ExperienceStore", Experience)
    monkeypatch.setattr(search_module, "SimulatorSearch", Search)
    monkeypatch.setattr(runner_module, "StrategicPlayer", Player)
    runner = BotRunner(BotConfig(username="Bot", battles=2, auto_update_meta=False,
                                 learning_enabled=False), lambda _: None, tmp_path)
    async def play(player, listen):
        played.append(player.search.meta.rating)
        player.current_rating = 1775
    monkeypatch.setattr(runner, "_play_one", play)
    asyncio.run(runner._run())
    assert played == [1695, 1825]
    assert loaded == [1695, 1825]
