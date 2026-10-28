"""Opponent scouting from earlier games."""
from poke_env.battle import Pokemon

from unaware.engine import DecisionEngine
from unaware.scouting import OpponentScout, scouted_set
from unaware.search import SimulatorSearch


def game(lead, moves, archetype="hyper_offense"):
    return {"source": "replay", "turns": 20, "won": True,
            "team": {"species": ["glimmora", "kingambit", "dragonite"], "lead_species": lead, "switches": 6,
                     "archetype": {"archetype": archetype},
                     "revealed": [{"species": "kingambit", "moves": moves, "items": ["blackglasses"],
                                   "abilities": ["supremeoverlord"], "tera_types": ["fairy"]}]}}


def test_profile_finds_usual_lead_team_and_sets():
    games = [game("glimmora", ["swordsdance", "suckerpunch"]), game("glimmora", ["swordsdance", "kowtowcleave"]),
             game("dragonite", ["ironhead"])]
    profile = OpponentScout.profile("Rival", "gen9ou", games)
    assert profile["games"] == 3
    assert max(profile["leads"], key=profile["leads"].get) == "glimmora"
    assert profile["archetype"] == "hyper_offense"
    seen = scouted_set(profile, "Kingambit")
    assert seen["moves"][0] == "swordsdance" and seen["item"] == "blackglasses" and seen["tera"] == "fairy"


def test_empty_history_is_harmless():
    profile = OpponentScout.profile("Nobody", "gen9ou", [])
    assert profile["games"] == 0 and scouted_set(profile, "kingambit") == {}


def test_search_hypothesis_uses_the_players_own_set():
    search = SimulatorSearch()
    search.opponent_scout = OpponentScout.profile("Rival", "gen9ou", [game("glimmora", ["swordsdance", "suckerpunch"])])
    mon = Pokemon(9, species="kingambit")
    data = search._set(mon, DecisionEngine(), 0, True)
    assert {"swordsdance", "suckerpunch"} <= set(data["set"]["moves"])
    assert data["set"]["item"] == "blackglasses" and data["set"]["teraType"] == "Fairy"


def test_switch_prior_follows_how_the_opponent_plays():
    from unaware.runner import foe_switch_prior
    stays = [{"observations": {"foe_switched": False}, "before": {}} for _ in range(8)]
    switches = [{"observations": {"foe_switched": True}, "before": {}} for _ in range(8)]
    assert foe_switch_prior({}, []) == .45
    assert foe_switch_prior({}, stays) < .3 < .7 < foe_switch_prior({}, switches)
    assert foe_switch_prior({"switch_rate": .1}, []) < .35


def test_hidden_items_are_inferred_from_behaviour():
    from unaware.scouting import infer_hidden
    log = "\n".join([
        "|switch|p2a: Rocks|Glimmora, F|100/100", "|switch|p1a: Wake|Walking Wake|100/100", "|turn|1",
        "|move|p2a: Rocks|Stealth Rock|p1a: Wake", "|-sidestart|p1: Me|move: Stealth Rock",
        "|move|p1a: Wake|Draco Meteor|p2a: Rocks", "|turn|2",
        "|move|p1a: Wake|Draco Meteor|p2a: Rocks", "|turn|3",
        "|move|p1a: Wake|Draco Meteor|p2a: Rocks", "|turn|4",
        "|switch|p1a: Alo|Alomomola|100/100", "|turn|5",  # no Stealth Rock damage -> Boots
        "|move|p1a: Alo|Wish|p1a: Alo", "|turn|6",
    ])
    hidden = infer_hidden(log, 0)
    assert "choicespecs" in hidden["walkingwake"]["items"]
    assert "heavydutyboots" in hidden["alomomola"]["items"]
    assert "choicespecs" not in hidden.get("alomomola", {}).get("items", {})
