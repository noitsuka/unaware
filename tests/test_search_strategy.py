"""Official-mechanics regressions with minimal, synthetic tactical positions.

No account, replay, usage cache or user team is needed by these fixtures.
"""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from poke_env.battle import Pokemon, PokemonType, SideCondition

from unaware.engine import Candidate, DecisionEngine
from unaware.search import SimulatorSearch
from unaware.strategy import analyze_team
from unaware.teams import TeamMember
from test_decision_regressions import battle, pokemon


@pytest.fixture(scope="module")
def worker():
    root = Path(__file__).parents[1]
    if not (root / "runtime/node_modules/pokemon-showdown").exists():
        pytest.skip("Requires the bundled official Pokémon Showdown simulator.")
    if not shutil.which("node") and not (root / "runtime/node.exe").exists():
        pytest.skip("Requires Node.js.")
    with SimulatorSearch(time_budget_ms=1500, depth=1) as search:
        search.healthcheck()
        yield search


@pytest.fixture
def search(worker):
    worker.feedback = {}
    worker.preferences = {}
    worker.strategy_profile = {}
    worker.time_budget_ms, worker.depth = 1500, 1
    worker.brain = "v2"
    return worker


def payload(search, state, engine=None):
    engine = engine or DecisionEngine(allow_tera=False)
    return search._payload(state, engine.rank_actions(state), engine)


def inspect(search, state, actions, *, data=None):
    request = data or payload(search, state)
    request.update(op="inspect", testActions=actions, sample=7)
    return search._rpc(request)


def kingambit(hp=1.0):
    return pokemon("kingambit", ("swordsdance", "kowtowcleave", "suckerpunch", "ironhead"),
                   [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord", "blackglasses", hp)


def skarmory(hp=1.0):
    return pokemon("skarmory", ("roost", "whirlwind", "bravebird", "bodypress"),
                   [252, 0, 252, 0, 4, 0], "impish", "sturdy", "rockyhelmet", hp,
                   tera=PokemonType.DRAGON)


def test_supreme_overlord_restores_four_observed_fallen_allies(search):
    own = kingambit()
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    dead = [pokemon(species, ("tackle",), [252, 0, 0, 0, 4, 0], "serious", "", hp=0)
            for species in ("mew", "garchomp", "dragonite", "greattusk")]
    state = battle(own, foe)
    base = inspect(search, state, [{"move": "kowtowcleave"}, {"move": "softboiled"}])
    state.team.update({f"p1: dead {i}": mon for i, mon in enumerate(dead)})
    boosted = inspect(search, state, [{"move": "kowtowcleave"}, {"move": "softboiled"}])
    original = base["sides"][1]["pokemon"][0]
    stronger = boosted["sides"][1]["pokemon"][0]
    base_damage = original["maxhp"] - original["hp"]
    boosted_damage = stronger["maxhp"] - stronger["hp"]
    assert 1.35 < boosted_damage / base_damage < 1.45
    assert boosted["sides"][0]["pokemonLeft"] == 1
    assert boosted["sides"][0]["pokemon"][0]["stats"] == base["sides"][0]["pokemon"][0]["stats"]


@pytest.mark.parametrize("counter,species,ability", [("whirlwind", "skarmory", "sturdy"), ("haze", "toxapex", "regenerator")])
def test_revealed_setup_denial_survives_beam_pruning_and_removes_boosts(search, counter, species, ability):
    own = kingambit()
    own.boosts["atk"] = 2
    foe = pokemon(species, ("roost" if species == "skarmory" else "recover", counter,
                            "bravebird" if species == "skarmory" else "scald", "bodypress" if species == "skarmory" else "knockoff"),
                  [252, 0, 252, 0, 4, 0], "impish", ability, "rockyhelmet", tera=PokemonType.DRAGON)
    bench = pokemon("greattusk", ("earthquake", "knockoff", "rapidspin"),
                    [252, 0, 252, 0, 4, 0], "impish", "protosynthesis")
    state = battle(own, foe, [bench])
    state.opponent_can_tera = True
    request = payload(search, state)
    request["op"] = "inspect"
    reconstructed = search._rpc(request)
    choices = reconstructed["options"][1]
    assert any(option.get("move") == counter and not option.get("tera") for option in choices), "Known denial must not disappear behind Tera duplicates or recovery."
    resolved = inspect(search, state, [{"move": "swordsdance"}, {"move": counter}])
    own_after = next(mon for mon in resolved["sides"][0]["pokemon"] if mon["species"] == "kingambit")
    if counter == "whirlwind":
        assert not own_after["active"]
        assert next(mon for mon in resolved["sides"][0]["pokemon"] if mon["species"] == "greattusk")["active"]
    assert own_after["boosts"]["atk"] == 0


def test_special_attack_stages_have_no_leaf_value_on_a_pure_physical_build(search):
    own = kingambit()
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state = battle(own, foe)
    engine = DecisionEngine(allow_tera=False)
    base = search.rank(state, [Candidate(own.moves["kowtowcleave"])], engine)[0].score
    own.boosts["spa"] = 6
    irrelevant = search.rank(state, [Candidate(own.moves["kowtowcleave"])], engine)[0].score
    assert irrelevant == pytest.approx(base, abs=.01)
    own.boosts.update(spa=0, atk=2)
    useful = search.rank(state, [Candidate(own.moves["kowtowcleave"])], engine)[0].score
    assert useful > base + 5


def test_relevant_negative_offensive_stages_keep_a_future_resource_cost(search):
    own = pokemon("gholdengo", ("shadowball", "recover"), [0, 0, 0, 252, 4, 252], "timid", "goodasgold")
    foe = pokemon("magikarp", ("splash", "flail", "tackle", "bounce"), [252, 0, 252, 0, 4, 0], "bold", "swiftswim")
    for move in ("flail", "tackle", "bounce"):
        foe.moves[move]._current_pp = 0
    state = battle(own, foe)
    request = payload(search, state)
    request["op"] = "inspect"
    neutral = search._rpc(request)["evaluation"]
    own.boosts["spa"] = -2
    request = payload(search, state)
    request["op"] = "inspect"
    weakened = search._rpc(request)["evaluation"]
    assert weakened < neutral - 10


def test_search_payload_carries_exact_build_strategy_profile(search):
    own = kingambit()
    foe = skarmory()
    profile = analyze_team([TeamMember("Kingambit", item="Black Glasses", ability="Supreme Overlord",
                                      moves=["Swords Dance", "Kowtow Cleave", "Sucker Punch", "Iron Head"],
                                      evs={"hp": 252, "atk": 252, "def": 0, "spa": 0, "spd": 4, "spe": 0})], "hyper_offense")
    search.strategy_profile = profile
    assert payload(search, battle(own, foe))["strategy"] == profile


def test_team_plan_changes_leaf_resource_value_without_changing_mechanics(search):
    own = pokemon("glimmora", ("memento", "stealthrock"), [0, 0, 0, 252, 4, 252], "timid", "toxicdebris", "focussash")
    reserve = kingambit()
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state = battle(own, foe, [reserve])
    profile = {"roles": [{"species": "glimmora", "roles": ["suicide_lead", "hazard_setter"]},
                         {"species": "kingambit", "roles": ["setup_sweeper", "cleaner"]}], "win_conditions": ["kingambit"]}
    engine = DecisionEngine(allow_tera=False)
    scores = {}
    search.brain = "legacy"  # archetype-dependent leaf value belongs to the legacy evaluation
    for style in ("hyper_offense", "stall"):
        search.strategy_profile = dict(profile, archetype=style)
        ranked = search.rank(state, [Candidate(own.moves["memento"])], engine)
        scores[style] = ranked[0].score
    assert scores["hyper_offense"] > scores["stall"] + 30


def test_observed_damage_filters_incompatible_hidden_sets_without_modifying_damage(search):
    own = pokemon("amoonguss", ("spore", "gigadrain"), [252, 0, 252, 0, 4, 0], "bold", "regenerator")
    foe = pokemon("lanturn", ("icebeam",), [0, 0, 0, 252, 4, 252], "modest", "voltabsorb", "choicespecs")
    state = battle(own, foe)
    request = payload(search, state)
    first = copy.deepcopy(request["opponents"][0])
    incompatible = copy.deepcopy(first)
    incompatible[0]["set"].update(item="", nature="calm", evs={"hp": 252, "atk": 0, "def": 4, "spa": 0, "spd": 252, "spe": 0})
    incompatible[0]["stats"] = {}
    request["opponents"] = [first, incompatible]
    observed = inspect(search, state, [{"move": "spore"}, {"move": "icebeam"}], data=copy.deepcopy(request))
    victim = observed["sides"][0]["pokemon"][0]
    amount = (victim["maxhp"] - victim["hp"]) / victim["maxhp"]
    assert .1 < amount < 1
    request["op"] = "search"
    request["feedback"] = {"observed_damage": [{"attacker": "foe", "move": "icebeam", "amount": amount,
                                                  "attacker_hp": 1.0, "censored": False}]}
    result = search._rpc(request)
    assert result["diagnostics"]["conditioned_hypotheses"] == [0]
    assert result["diagnostics"]["observed_damage_samples"] == 1
    repeated = inspect(search, state, [{"move": "spore"}, {"move": "icebeam"}], data=copy.deepcopy(request))
    assert repeated["sides"][0]["pokemon"][0]["hp"] == victim["hp"], "Evidence must select a set, never scale the simulator formula."


def test_last_low_hp_attacker_does_not_spend_turn_on_known_ground_immunity(search):
    own = pokemon("greattusk", ("earthquake", "knockoff", "rapidspin", "stealthrock"),
                  [252, 0, 252, 0, 4, 0], "impish", "protosynthesis", "heavydutyboots", hp=.08)
    foe = skarmory(.2)
    reserve = pokemon("blissey", ("softboiled", "seismictoss"), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state = battle(own, foe)
    state.opponent_team["p2: reserve"] = reserve
    state.opponent_can_tera = False
    engine = DecisionEngine(allow_tera=False)
    candidates = [Candidate(own.moves["earthquake"]), Candidate(own.moves["knockoff"])]
    ranked = search.rank(state, candidates, engine)
    assert ranked[0].action.id == "knockoff"


def test_official_preview_receives_strategy_profile_and_remains_matchup_sensitive(search):
    lead = pokemon("glimmora", ("stealthrock", "spikes", "mortalspin", "memento"),
                   [0, 0, 0, 252, 4, 252], "timid", "toxicdebris", "focussash")
    attacker = kingambit()
    foe = pokemon("mew", ("psychic", "psyshock", "futuresight", "softboiled"), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    state = battle(lead, foe, [attacker])
    profile = {"archetype": "hyper_offense", "roles": [
        {"species": "glimmora", "roles": ["suicide_lead", "hazard_setter"]},
        {"species": "kingambit", "roles": ["setup_sweeper", "cleaner"]}],
        "win_conditions": ["kingambit"], "lead_species": "glimmora"}
    search.strategy_profile = profile
    captured, responses = [], []
    original = search._rpc
    def capture(request):
        captured.append(copy.deepcopy(request))
        result = original(request)
        responses.append(copy.deepcopy(result))
        return result
    search._rpc = capture
    try:
        order = search.preview_order(state, DecisionEngine(allow_tera=False))
        search.strategy_profile = dict(profile, archetype="stall")
        search.preview_order(state, DecisionEngine(allow_tera=False))
    finally:
        search._rpc = original
    assert captured[0]["strategy"] == profile
    assert captured[1]["strategy"]["archetype"] == "stall"
    assert order[0] == 2, "Dark/Steel immunity to the revealed attacks must dominate the preferred suicide lead."
    lead_values = [next(row["score"] for row in response["ranking"] if row["slot"] == 1) for response in responses]
    assert abs(lead_values[0] - lead_values[1]) > 10, "Preview must use the plan in its leaf evaluation."
    assert search.last_diagnostics["engine"] == "official-pokemon-showdown"
    assert search.last_diagnostics["nodes"] > 0


def test_preview_lead_weights_use_scouting_then_population_prior():
    from poke_env.battle import Pokemon
    from unaware.search import SimulatorSearch
    search = SimulatorSearch.__new__(SimulatorSearch)
    foes = [Pokemon(9, species=name) for name in ("kingambit", "ninetales", "gholdengo")]
    search.opponent_scout = {}
    prior = search._lead_weights(foes)
    assert abs(sum(prior) - 1) < .01 and prior[1] == max(prior)  # Ninetales: weather lead
    search.opponent_scout = {"leads": {"gholdengo": .8}}
    assert search._lead_weights(foes) == [0.0, 0.0, .8]
