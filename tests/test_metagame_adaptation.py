"""Population priors track ladder context without inventing opponent sets."""
from copy import deepcopy

import pytest

from unaware.meta import MetaSnapshot, MetaStore, blend_preferences, rating_bucket
from unaware.learning import ExperienceStore
from unaware.progress import _counter_available


@pytest.mark.parametrize("rating,cutoff", [(None, 0), (1000, 0), (1399, 0), (1400, 1500),
    (1624, 1500), (1625, 1695), (1774, 1695), (1775, 1825), (2100, 1825)])
def test_rating_population_changes_at_known_bands(rating, cutoff):
    assert rating_bucket(rating) == cutoff


def test_profiles_keep_previous_population_and_nearest_published_cutoff(tmp_path):
    store = MetaStore(tmp_path)
    lower = MetaSnapshot("gen9ru", "2026-08", 1500, "https://www.smogon.com/stats/2026-08/chaos/gen9ru-1500.json", {"mimikyu": {"Moves": {"Shadow Sneak": 20}}})
    higher = MetaSnapshot("gen9ru", "2026-08", 1630, "https://www.smogon.com/stats/2026-08/chaos/gen9ru-1630.json", {"mimikyu": {"Moves": {"Swords Dance": 30}}})
    store._save(lower, 1500)
    store._save(higher, 1695)
    assert store.load("gen9ru", 1500).species("Mimikyu")["Moves"] == {"Shadow Sneak": 20}
    assert store.load("gen9ru", 1695).rating == 1630
    assert store.load("gen9ru").rating == 1630


def test_public_research_is_bounded_and_never_overwrites_local_input():
    local = {"skarmory": {"confidence": 1, "moves": {"whirlwind": 1}, "battles": 12}}
    public = {"species": {"skarmory": {"confidence": 1, "moves": {"roost": 1}, "battles": 50}}}
    before = deepcopy((local, public))
    result = blend_preferences(local, public)
    assert (local, public) == before
    assert result["skarmory"]["confidence"] <= .25
    assert result["skarmory"]["moves"]["whirlwind"] > result["skarmory"]["moves"]["roost"]
    assert blend_preferences({}, public)["skarmory"]["confidence"] <= .15


def test_observed_priors_are_scoped_to_account_server_tier_and_rating(tmp_path):
    store = ExperienceStore(tmp_path)
    for i, (user, server, rating, move, tier) in enumerate([
        ("Bot", "local", 1100, "toxic", "gen9ru"), ("Bot", "local", 1830, "swordsdance", "gen9ru"),
        ("Other", "local", 1100, "icebeam", "gen9ru"), ("Bot", "elsewhere", 1100, "surf", "gen9ru"),
        ("Bot", "local", 1100, "recover", "gen9ou")]):
        store.record_battle({"battle_id": str(i), "server_namespace": server, "username": user,
            "format_id": tier, "rating_before": rating, "team_id": "team", "result": "win"},
            [{"observations": {"opponent_moves": [{"actor": "p2: Mimikyu", "move": move}]},
              "before": {"foe_roster": {"p2: Mimikyu": {"species": "mimikyu"}}}}])
    priors = store.preferences("gen9ru", rating=1100, username="Bot", server_namespace="local")
    assert priors["species"]["mimikyu"]["moves"] == {"toxic": 1}
    assert store.preferences("gen9ru", rating=1830, username="Bot", server_namespace="local")["species"]["mimikyu"]["moves"] == {"swordsdance": 1}


def test_phazing_counter_is_unavailable_against_last_remaining_pokemon():
    state = {"own_alive": 1, "foe_id": "p2: Skarmory", "foe_roster": {
        "p2: Skarmory": {"moves": {"whirlwind": {"pp": 8}}}}}
    assert not _counter_available(state, "whirlwind")
    state["own_alive"] = 2
    assert _counter_available(state, "whirlwind")
