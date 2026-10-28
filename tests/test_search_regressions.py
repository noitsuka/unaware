"""Mechanics and tactical regressions for state rebuilt in the official engine."""
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from poke_env.battle import Effect, PokemonType, SideCondition
from poke_env.battle import Pokemon

from unaware.engine import DecisionEngine
from unaware.search import SimulatorSearch
from test_decision_regressions import battle, cinccino, cloyster, mimikyu, mudsdale, pokemon


@pytest.fixture
def search():
    runtime = Path(__file__).parents[1] / "runtime/node_modules/pokemon-showdown"
    if not shutil.which("node") or not runtime.exists():
        pytest.skip("Requires the locally installed official Showdown simulator and Node.")
    with SimulatorSearch(time_budget_ms=2000) as worker:
        worker.healthcheck()
        yield worker


def inspect(search, state, actions=None):
    engine = DecisionEngine()
    payload = search._payload(state, engine.rank_actions(state), engine)
    payload["op"] = "inspect"
    if actions:
        payload["testActions"] = actions
    return search._rpc(payload)


@pytest.mark.parametrize("active_turns,should_work", [(1, True), (4, False)])
def test_fake_out_respects_active_move_actions_after_reconstruction(search, active_turns, should_work):
    own = pokemon("meowth", ("fakeout", "return"), [0, 252, 0, 0, 4, 252], "jolly", "technician")
    own._active_turns = active_turns
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure", hp=0.8)
    result = inspect(search, battle(own, foe), [{"move": "fakeout"}, {"move": "softboiled"}])
    failed = any(line.startswith("|-fail|p1") for line in result["log"])
    assert failed is not should_work
    assert any("|cant|p2" in line and "flinch" in line for line in result["log"]) is should_work


def test_choice_lock_retained_without_erasing_move_slots(search):
    own, foe = cinccino(), mudsdale()
    own._active_turns = 3
    own.moves["bulletseed"]._is_last_used = True
    state = battle(own, foe, available_moves=[own.moves["bulletseed"]])
    result = inspect(search, state)
    slots = result["sides"][0]["pokemon"][0]["moves"]
    assert {slot["id"] for slot in slots} == {"bulletseed", "tailslap", "rockblast", "uturn"}
    requested = result["sides"][0]["request"]["active"][0]["moves"]
    assert [move["id"] for move in requested if not move.get("disabled")] == ["bulletseed"]


def test_quark_drive_observed_speed_boost_is_not_discarded(search):
    own = pokemon("ironvaliant", ("moonblast", "closecombat"), [0, 0, 4, 252, 0, 252], "timid", "quarkdrive")
    own._effects[Effect.QUARKDRIVESPE] = 1
    foe = pokemon("mew", ("psychic",), [252, 0, 0, 0, 0, 252], "timid", "synchronize")
    result = inspect(search, battle(own, foe))
    assert "quarkdrive" in result["sides"][0]["pokemon"][0]["effects"]
    assert not any("quarkdrivespe" in warning for warning in result["warnings"])


def test_protosynthesis_observed_attack_boost_is_not_discarded(search):
    own = pokemon("greattusk", ("headlongrush", "closecombat"), [0, 252, 4, 0, 0, 252], "adamant", "protosynthesis")
    own._effects[Effect.PROTOSYNTHESISATK] = 1
    foe = pokemon("mew", ("psychic",), [252, 0, 0, 0, 0, 252], "timid", "synchronize")
    result = inspect(search, battle(own, foe))
    assert "protosynthesis" in result["sides"][0]["pokemon"][0]["effects"]


def test_opponent_defensive_tera_with_roost_is_a_considered_response(search):
    own = pokemon("primarina", ("moonblast",), [0, 0, 0, 252, 4, 252], "modest", "torrent")
    foe = pokemon("hydreigon", ("roost",), [252, 0, 0, 0, 4, 252], "timid", "levitate", hp=0.8,
                  tera=PokemonType.STEEL)
    state = battle(own, foe)
    engine = DecisionEngine(allow_tera=False)
    candidates = search.rank(state, engine.rank_actions(state), engine)
    assert candidates[0].action.id == "moonblast"
    # Fairy would always KO the original Dark/Dragon, but Steel Tera + Roost
    # survives. The search must not report every sampled response as a KO.
    assert candidates[0].diagnostics["ko_chance"] < 1.0


def test_already_terastallized_pokemon_keeps_defensive_type_and_spent_resource(search):
    own = pokemon("toxtricity", ("boomburst",), [0, 0, 0, 252, 4, 252], "modest", "punkrock",
                  tera=PokemonType.NORMAL)
    own._terastallized = True
    foe = mudsdale()
    result = inspect(search, battle(own, foe, can_tera=False))
    active = result["sides"][0]["pokemon"][0]
    assert active["tera"] == "Normal"
    assert not result["sides"][0]["request"]["active"][0].get("canTerastallize")


def test_forced_replacement_preserves_unused_tera_for_its_following_attack(search):
    """The server cannot offer Tera while choosing a replacement; it is unspent."""
    dead, foe = cinccino(hp=0), mudsdale(hp=0.84)
    replacement = pokemon("toxtricity", ("boomburst", "overdrive"), [0, 0, 0, 252, 4, 252],
                          "modest", "punkrock", "throatspray", tera=PokemonType.NORMAL)
    state = battle(dead, foe, [replacement], force_switch=True, can_tera=False)
    result = inspect(search, state, [{"switch": 1}, {"wait": True}])
    request = result["sides"][0]["request"]["active"][0]
    assert request.get("canTerastallize") == "Normal"


def test_user_disabled_tera_is_not_assumed_available_in_future_search(search):
    own, foe = mimikyu(), mudsdale()
    state = battle(own, foe, can_tera=True)
    engine = DecisionEngine(allow_tera=False)
    payload = search._payload(state, engine.rank_actions(state), engine)
    payload["op"] = "inspect"
    result = search._rpc(payload)
    assert not result["sides"][0]["request"]["active"][0].get("canTerastallize")


def test_forced_replacement_hazards_are_resolved_before_leaf(search):
    dead, foe = cinccino(hp=0), mudsdale()
    replacement = pokemon("charizard", ("flamethrower",), [0, 0, 0, 252, 4, 252], "timid", "blaze", hp=0.4)
    state = battle(dead, foe, [replacement], force_switch=True,
                   side_conditions={SideCondition.STEALTH_ROCK: 1})
    result = inspect(search, state, [{"switch": 1}, {"wait": True}])
    assert result["ended"]
    assert result["winner"] == "Opponent"
    assert any("[from] Stealth Rock" in line for line in result["log"])


def test_search_forced_replacement_values_lethal_priority_over_unusable_damage(search):
    dead, foe = cloyster(hp=0), cloyster(hp=0.20, smashed=True)
    ordinary, priority = cinccino(hp=256 / 292), mimikyu()
    state = battle(dead, foe, [ordinary, priority], force_switch=True,
                   side_conditions={SideCondition.STEALTH_ROCK: 1})
    engine = DecisionEngine(allow_tera=False)
    ranked = search.rank(state, engine.rank_actions(state), engine)
    assert ranked[0].action is priority


def test_next_turn_beam_keeps_winning_triage_attack_after_forced_replacement(search):
    """All three attacks KO, but only Draining Kiss acts before the sweeper.

    A raw-power beam that ignores ability priority pruned this winning line
    and returned a certain-loss evaluation of about -8500.
    """
    dead = cinccino(hp=0)
    priority = pokemon("comfey", ("storedpower", "dazzlinggleam", "drainingkiss", "calmmind"),
                       [236, 0, 0, 252, 0, 20], "modest", "triage", "lifeorb")
    foe = pokemon("porygonz", ("triattack",), [0, 0, 0, 252, 4, 252], "timid",
                  "adaptability", "choicespecs", hp=0.05)
    foe.boosts.update(spa=2, spe=2)
    state = battle(dead, foe, [priority], force_switch=True)
    engine = DecisionEngine(allow_tera=False)
    ranked = search.rank(state, engine.rank_actions(state), engine)
    assert ranked[0].action is priority
    assert ranked[0].diagnostics["search_depth"] == 2
    assert ranked[0].score > 1000, "The only winning priority attack must remain in the beam."


def test_grassy_glide_move_callback_priority_keeps_winning_continuation(search):
    """Grassy Glide's +1 comes from the move callback, not an ability callback."""
    dead = cinccino(hp=0)
    priority = pokemon("rillaboom", ("woodhammer", "drainpunch", "grassyglide", "swordsdance"),
                       [0, 252, 4, 0, 0, 252], "adamant", "grassysurge", "lifeorb")
    foe = pokemon("porygonz", ("triattack",), [0, 0, 0, 252, 4, 252], "timid",
                  "adaptability", "choicespecs", hp=0.05)
    foe.boosts.update(spa=2, spe=2)
    state = battle(dead, foe, [priority], force_switch=True)
    engine = DecisionEngine(allow_tera=False)
    ranked = search.rank(state, engine.rank_actions(state), engine)
    assert ranked[0].action is priority
    assert ranked[0].score > 1000, "Grassy Glide must survive beam pruning in its new terrain."


def test_repeatedly_observed_coverage_can_enter_hypotheses_when_absent_from_monthly_usage():
    """Learning a rare move must do more than reweight the monthly move list."""
    data = {"Moves": {"earthquake": 100.0, "bodypress": 90.0,
                      "stealthrock": 80.0, "rockslide": 70.0},
            "Spreads": {"Impish:252/0/252/0/0/4": 100.0},
            "Abilities": {"Stamina": 100.0}, "Items": {"Leftovers": 100.0}}
    meta = SimpleNamespace(species=lambda species: data if species == "mudsdale" else {})
    hidden = Pokemon(9, species="mudsdale")
    policy = DecisionEngine(meta=meta)
    evaluator = SimulatorSearch(meta=meta)
    evaluator.preferences = {"mudsdale": {"battles": 4, "confidence": 0.3333,
                                         "moves": {"heavyslam": 1.0}}}
    hypotheses = [evaluator._set(hidden, policy, index, opponent=True) for index in range(3)]
    assert any("heavyslam" in item["set"]["moves"] for item in hypotheses)
    assert all(len(item["set"]["moves"]) <= 4 for item in hypotheses)


def test_six_vs_six_budget_returns_all_legal_root_actions_without_timeout(search):
    own, foe = cinccino(), mudsdale()
    own_bench = [mimikyu(), cloyster(),
                 pokemon("kleavor", ("stoneaxe", "xscissor", "closecombat", "swordsdance"),
                         [0, 252, 4, 0, 0, 252], "jolly", "sharpness", "focussash"),
                 pokemon("toxtricity", ("boomburst", "sludgewave", "overdrive", "shiftgear"),
                         [0, 0, 0, 252, 4, 252], "modest", "punkrock", "throatspray"),
                 pokemon("comfey", ("drainingkiss", "storedpower", "taunt", "calmmind"),
                         [236, 0, 0, 252, 0, 20], "modest", "triage", "lifeorb")]
    state = battle(own, foe, own_bench)
    state.opponent_team.update({f"p2: {index}": mon for index, mon in enumerate([
        cloyster(), pokemon("flamigo", ("closecombat", "bravebird", "uturn", "roost"),
                            [0, 252, 0, 0, 4, 252], "jolly", "scrappy", "choicescarf"),
        pokemon("porygonz", ("triattack", "darkpulse", "icebeam", "trick"),
                [0, 0, 0, 252, 4, 252], "timid", "adaptability", "choicescarf"),
        pokemon("primeape", ("ragefist", "drainpunch", "bulkup", "rest"),
                [252, 0, 0, 0, 252, 4], "careful", "defiant", "eviolite"),
        pokemon("toxtricity", ("boomburst", "sludgewave", "overdrive", "shiftgear"),
                [0, 0, 0, 252, 4, 252], "modest", "punkrock", "throatspray")])})
    search.time_budget_ms = 200
    engine = DecisionEngine()
    roots = engine.rank_actions(state)
    started = time.monotonic()
    ranked = search.rank(state, roots, engine)
    elapsed = time.monotonic() - started
    assert len(ranked) == len(roots)
    assert all(candidate.diagnostics["search_legal"] == 1 for candidate in ranked)
    assert elapsed < 3.0, "Mandatory first round must stay bounded for a full roster."
