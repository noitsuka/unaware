"""Manual team editing and style metadata in isolated storage."""
import copy
import json

import pytest

from unaware.backend import AppService
from tests.test_backend import TEAM, api, service
CORE = """Mimikyu @ Life Orb
Ability: Disguise
Tera Type: Ghost
EVs: 252 Atk / 4 SpD / 252 Spe
Jolly Nature
- Swords Dance
- Play Rough
- Shadow Sneak
- Shadow Claw

Ribombee @ Focus Sash
Ability: Shield Dust
Tera Type: Ghost
EVs: 252 SpA / 4 SpD / 252 Spe
Timid Nature
IVs: 0 Atk
- Sticky Web
- Moonblast
- Stun Spore
- Skill Swap"""


def test_saved_tier_and_archetype_survive_restart_and_profile_uses_builds(service):
    service.save_team({"name": "Custom Webs", "text": CORE, "format_id": "gen9ru", "archetype": "hyper_offense"})
    team = service.teams()[0]
    assert team["format_id"] == "gen9ru" and team["archetype"] == "hyper_offense"
    assert team["text"] == CORE + "\n"
    assert team["profile"]["archetype"] == "hyper_offense"
    assert team["profile"]["lead_species"] == "ribombee"
    assert "mimikyu" in team["profile"]["win_conditions"]
    reopened = AppService(service.base_dir)
    try:
        assert reopened.teams()[0]["archetype"] == "hyper_offense"
        assert reopened.teams()[0]["format_id"] == "gen9ru"
        assert reopened.config.archetype == "hyper_offense"
        assert reopened.config.format_id == "gen9ru"
    finally:
        reopened.close()


def test_rename_retains_metadata_and_archives_original_export(service):
    service.save_team({"name": "Webs", "text": CORE, "format_id": "gen9ru", "archetype": "hyper_offense"})
    service.save_team({"name": "Renamed", "original_name": "Webs.txt", "text": CORE, "format_id": "gen9ru", "archetype": "hyper_offense"})
    assert not (service.team_dir / "Webs.txt").exists()
    assert (service.team_dir / "Renamed.txt").read_text(encoding="utf-8") == CORE + "\n"
    assert service.team_metadata == {"Renamed.txt": {"format_id": "gen9ru", "archetype": "hyper_offense"}}
    archived = list((service.team_dir / ".trash").glob("*Webs.txt"))
    assert len(archived) == 1 and archived[0].read_text(encoding="utf-8") == CORE + "\n"


def test_deleting_team_removes_only_its_profile(service):
    service.save_team({"name": "Webs", "text": CORE, "format_id": "gen9ru", "archetype": "hyper_offense"})
    service.save_team({"name": "Dragon", "text": TEAM, "format_id": "gen9ou", "archetype": "balance"})
    service.delete_team({"name": "Webs.txt"})
    assert "Webs.txt" not in service.team_metadata
    assert service.team_metadata["Dragon.txt"]["archetype"] == "balance"
    assert (service.team_dir / "Dragon.txt").exists()


@pytest.mark.parametrize("changes", [{"format_id": "gen8ou"}, {"format_id": "gen9randombattle"}, {"archetype": "not-a-style"}])
def test_invalid_style_or_tier_does_not_save_partial_metadata(service, changes):
    original_config = copy.deepcopy(service.config)
    with pytest.raises(ValueError):
        service.save_team({"name": "Bad", "text": CORE, **changes})
    assert not (service.team_dir / "Bad.txt").exists()
    assert service.team_metadata == {}
    assert service.config == original_config


def test_analyze_endpoint_returns_real_roles_without_saving_team(api):
    request, _ = api
    status, body, _ = request("/api/team/analyze", "POST", {"text": CORE, "archetype": "hyper_offense"}, token=True)
    assert status == 200
    data = json.loads(body)
    profile = data["profile"]
    assert profile["archetype"] == "hyper_offense"
    assert profile["lead_species"] == "ribombee"
    assert "setup_sweeper" in next(row["roles"] for row in profile["roles"] if row["species"] == "mimikyu")


def test_removed_recommendation_route_does_not_modify_manual_team(api, service):
    request, _ = api
    service.save_team({"name": "Original", "text": CORE, "format_id": "gen9ru", "archetype": "hyper_offense"})
    before = (service.team_dir / "Original.txt").read_bytes()
    assert request("/api/team/recommend", "POST", {"text": CORE}, token=True)[0] == 404
    assert (service.team_dir / "Original.txt").read_bytes() == before
