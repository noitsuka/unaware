"""Tactical and mechanics regressions against the official forward simulator."""

import pytest
from poke_env.battle import Effect, Field, Move, Pokemon, SideCondition, Status, Weather
from poke_env.teambuilder import Teambuilder

from unaware.engine import Candidate, DecisionEngine
from unaware.search import SearchUnavailable, SimulatorSearch
from tests.test_engine import Meta, battle, engine_for, pokemon


@pytest.fixture(scope="module")
def search():
    with SimulatorSearch(time_budget_ms=1500) as worker:
        worker.ready()
        yield worker


def inspect(search, own, foe, own_action=None, foe_action=None, **kwargs):
    policy = engine_for(foe)
    arena = battle(own, foe, opponent_can_tera=False, **kwargs)
    payload = search._payload(arena, policy.rank_actions(arena), policy)
    payload["op"] = "inspect"
    if own_action is not None:
        payload["testActions"] = [own_action, foe_action]
    return search._rpc(payload)


def choose(search, arena, policy=None):
    policy = policy or engine_for(arena.opponent_active_pokemon)
    return search.rank(arena, policy.rank_actions(arena), policy)[0]


def from_export(text):
    mon = Pokemon(9, teambuilder=Teambuilder.parse_showdown_team(text)[0])
    snapshot = DecisionEngine()._snapshot(mon)
    mon._stats = dict(snapshot.stats)
    mon._max_hp = snapshot.max_hp
    mon._current_hp = snapshot.max_hp
    mon._active_turns = 1
    return mon


def test_worker_ready_and_official_tier_validation(search):
    assert search.ready()["engine"] == "official-pokemon-showdown"
    errors = search.validate_team("Mewtwo\nAbility: Pressure\n- Psychic", "gen9ou")
    assert errors and any("Mewtwo" in line for line in errors)
    team = "Great Tusk @ Booster Energy\nAbility: Protosynthesis\nEVs: 252 Atk / 4 Def / 252 Spe\nJolly Nature\n- Rapid Spin\n- Headlong Rush\n- Close Combat\n- Ice Spinner"
    assert search.validate_team(team, "gen9ou") == []


def test_missing_node_is_an_explicit_failure():
    with SimulatorSearch(node_path="X:/does-not-exist/node.exe") as worker:
        with pytest.raises(SearchUnavailable, match="start the simulator"):
            worker.ready()


def test_own_exact_evs_ivs_nature_stats_and_pp_survive_reconstruction(search):
    own = from_export("Slowking-Galar @ Assault Vest\nAbility: Regenerator\nEVs: 252 HP / 4 Def / 252 SpD\nIVs: 0 Atk / 0 Spe\nSassy Nature\nTera Type: Water\n- Future Sight\n- Sludge Bomb\n- Ice Beam\n- Flamethrower")
    own._current_hp = own.max_hp // 2
    own._boosts["spd"] = 2
    own.status = Status.PAR
    own.moves["icebeam"].use()
    foe = pokemon("blissey", ["seismictoss"])
    snapshot = inspect(search, own, foe)
    actual = snapshot["sides"][0]["pokemon"][0]
    assert actual["stats"] == {key: value for key, value in own.stats.items() if key != "hp"}
    assert actual["hp"] == own.current_hp
    assert actual["maxhp"] == own.max_hp
    assert actual["status"] == "par" and actual["boosts"]["spd"] == 2
    assert next(slot["pp"] for slot in actual["moves"] if slot["id"] == "icebeam") == own.moves["icebeam"].current_pp


def test_ground_immunity_is_not_overridden_by_high_greedy_score(search):
    own = pokemon("starmie", ["thunderbolt", "icebeam"])
    own._stats["spe"] = 500
    foe = pokemon("landorustherian", ["earthquake"])
    arena = battle(own, foe, opponent_can_tera=False)
    candidates = [Candidate(own.moves["thunderbolt"], score=999999), Candidate(own.moves["icebeam"], score=-999999)]
    ranked = search.rank(arena, candidates, engine_for(foe))
    assert ranked[0].action.id == "icebeam"
    assert ranked[1].diagnostics["expected_dealt"] == 0
    assert ranked[0].diagnostics["search_depth"] == 2


def test_skill_link_runs_five_hits_and_stamina_changes_each_later_hit(search):
    own = pokemon("cloyster", ["iciclespear"], ability="skilllink")
    own._stats.update(atk=250, spe=500)
    foe = pokemon("mudsdale", ["splash"], ability="stamina")
    snapshot = inspect(search, own, foe, {"move": "iciclespear"}, {"move": "splash"})
    target = snapshot["sides"][1]["pokemon"][0]
    assert target["boosts"]["def"] == 5
    assert any("|-hitcount|p2a: foe0|5" in line for line in snapshot["log"])
    assert 0 < target["hp"] < target["maxhp"]
    boost_lines = [line for line in snapshot["log"] if "|-boost|p2a: foe0|def|1" in line]
    assert len(boost_lines) == 5


def test_disguise_blocks_only_first_hit_not_entire_skill_link_move(search):
    own = from_export("Cloyster\nAbility: Skill Link\nEVs: 252 Atk / 4 Def / 252 Spe\nJolly Nature\n- Icicle Spear")
    foe = from_export("Mimikyu\nAbility: Disguise\nEVs: 252 HP / 252 Def / 4 SpD\nImpish Nature\n- Splash")
    own._stats["spe"] = 500
    one = inspect(search, own, foe, {"move": "iciclespear"}, {"move": "splash"})
    target = one["sides"][1]["pokemon"][0]
    assert target["species"] == "mimikyubusted"
    assert target["hp"] < foe.max_hp * .875
    assert any("|-activate|p2a: foe0|ability: Disguise" in line for line in one["log"])
    assert any("|-hitcount|p2a: foe0|5" in line for line in one["log"])


def test_fakeout_and_firstimpression_fail_after_first_active_action(search):
    for species, move_id in [("ironhands", "fakeout"), ("lokix", "firstimpression")]:
        own = pokemon(species, [move_id, "closecombat"])
        own._active_turns = 4
        foe = pokemon("blissey", ["splash"])
        snapshot = inspect(search, own, foe, {"move": move_id}, {"move": "splash"})
        assert snapshot["sides"][1]["pokemon"][0]["hp"] == foe.max_hp
        assert any("|-fail|p1a: own0" in line for line in snapshot["log"])


def test_fakeout_still_works_on_arrival(search):
    own = pokemon("ironhands", ["fakeout"])
    own._active_turns = 1
    foe = pokemon("blissey", ["splash"])
    snapshot = inspect(search, own, foe, {"move": "fakeout"}, {"move": "splash"})
    assert snapshot["sides"][1]["pokemon"][0]["hp"] < foe.max_hp
    assert any("|cant|p2a: foe0|flinch" in line for line in snapshot["log"])


def test_priority_takes_secure_ko_before_faster_opponent(search):
    own = pokemon("kingambit", ["suckerpunch", "ironhead"], hp=.15)
    own._stats["spe"] = 100
    foe = pokemon("dragapult", ["flamethrower"], hp=.10)
    foe._stats["spe"] = 450
    choice = choose(search, battle(own, foe, opponent_can_tera=False))
    assert choice.action.id == "suckerpunch"
    assert choice.diagnostics["faint_risk"] == 0
    assert choice.diagnostics["ko_chance"] == 1


def test_triage_priority_finisher_is_not_pruned_from_second_turn_beam(search):
    dead = pokemon("pikachu", ["thunderbolt"], hp=0)
    dead.status = Status.FNT
    own = from_export("Comfey @ Life Orb\nAbility: Triage\nEVs: 236 HP / 252 SpA / 20 Spe\nModest Nature\n- Stored Power\n- Dazzling Gleam\n- Draining Kiss\n- Calm Mind")
    foe = from_export("Porygon-Z @ Choice Specs\nAbility: Adaptability\nEVs: 252 SpA / 4 SpD / 252 Spe\nTimid Nature\n- Tri Attack")
    foe._current_hp = round(foe.max_hp * .05)
    foe._boosts.update(spa=2, spe=2)
    arena = battle(dead, foe, [own], force_switch=True, opponent_can_tera=False)
    chosen = choose(search, arena)
    assert chosen.action is own
    assert chosen.diagnostics["search_depth"] == 2
    # Draining Kiss's official +3 priority is the only winning continuation.
    assert chosen.score > 5000


def test_does_not_setup_when_faster_opponent_will_ko_it(search):
    own = pokemon("cloyster", ["shellsmash", "iceshard"], hp=.12, ability="skilllink")
    own._stats["spe"] = 150
    foe = pokemon("dragapult", ["thunderbolt"], hp=.04)
    foe._stats["spe"] = 450
    assert choose(search, battle(own, foe, opponent_can_tera=False)).action.id == "iceshard"


def test_full_health_recover_is_not_selected_over_a_winning_attack(search):
    own = pokemon("starmie", ["recover", "surf"])
    own._stats["spe"] = 500
    foe = pokemon("charizard", ["flamethrower"], hp=.2)
    assert choose(search, battle(own, foe, opponent_can_tera=False)).action.id == "surf"


def test_hazards_and_boots_apply_to_forced_replacement(search):
    dead = pokemon("pikachu", ["thunderbolt"], hp=0)
    dead.status = Status.FNT
    foe = pokemon("blissey", ["splash"])
    regular = pokemon("charizard", ["flamethrower"])
    boots = pokemon("moltres", ["flamethrower"], item="heavydutyboots")
    policy = engine_for(foe)
    arena = battle(dead, foe, [regular, boots], force_switch=True,
                   side_conditions={SideCondition.STEALTH_ROCK: 1}, opponent_can_tera=False)
    payload = search._payload(arena, policy.rank_actions(arena), policy)
    payload.update(op="inspect", testActions=[{"switch": 1}, {"wait": True}])
    rock = search._rpc(payload)
    active = next(mon for mon in rock["sides"][0]["pokemon"] if mon["active"])
    assert active["name"] == "own1" and active["hp"] == regular.max_hp // 2
    payload["testActions"][0] = {"switch": 2}
    clear = search._rpc(payload)
    active = next(mon for mon in clear["sides"][0]["pokemon"] if mon["active"])
    assert active["name"] == "own2" and active["hp"] == boots.max_hp


def test_u_turn_resolves_replacement_before_queued_enemy_attack(search):
    own = pokemon("dragapult", ["uturn"])
    own._stats["spe"] = 500
    bench = pokemon("corviknight", ["bravebird"], ability="pressure")
    foe = pokemon("garchomp", ["earthquake"])
    snapshot = inspect(search, own, foe, {"move": "uturn"}, {"move": "earthquake"}, bench=[bench])
    old = next(mon for mon in snapshot["sides"][0]["pokemon"] if mon["name"] == "own0")
    active = next(mon for mon in snapshot["sides"][0]["pokemon"] if mon["active"])
    assert old["hp"] == own.max_hp
    assert active["name"] == "own1" and active["hp"] == bench.max_hp
    assert any("|-immune|p1a: own1" in line for line in snapshot["log"])


def test_weather_terrain_and_trickroom_are_reconstructed(search):
    own = pokemon("pelipper", ["surf"], ability="drizzle")
    foe = pokemon("slowking", ["psychic"])
    snapshot = inspect(search, own, foe, weather={Weather.RAINDANCE: 1},
                       fields={Field.TRICK_ROOM: 1, Field.GRASSY_TERRAIN: 1})
    assert snapshot["weather"] == "raindance"
    assert snapshot["terrain"] == "grassyterrain"
    assert "trickroom" in snapshot["fields"]


def test_paradox_observed_speed_boost_is_retained_after_booster_consumed(search):
    own = pokemon("ironvaliant", ["moonblast"], ability="quarkdrive", item=None)
    own._effects[Effect.QUARKDRIVESPE] = 0
    foe = pokemon("dragapult", ["shadowball"])
    own._stats.update(spa=300, spe=300)
    foe._stats["spe"] = 400
    own._current_hp = 1
    foe._current_hp = 1
    snapshot = inspect(search, own, foe, {"move": "moonblast"}, {"move": "shadowball"})
    assert snapshot["winner"] == "Bot"
    assert any("|move|p1a: own0|Moonblast" in line for line in snapshot["log"])
    assert not any("Effect not rebuilt: quarkdrivespe" in warning for warning in snapshot["warnings"])


def test_unknown_hypotheses_never_give_eight_moves_or_lose_revealed_moves(search):
    own = pokemon("starmie", ["surf"])
    foe = Pokemon(9, species="kingambit")
    foe.moves["suckerpunch"] = Move("suckerpunch", gen=9)
    meta = Meta({"kingambit": {"Spreads": {"Adamant:252/252/0/0/4/0": .6,
                                             "Jolly:0/252/4/0/0/252": .4},
                               "Moves": {"Sucker Punch": .95, "Kowtow Cleave": .9,
                                         "Iron Head": .8, "Swords Dance": .7,
                                         "Low Kick": .5, "Tera Blast": .2}}})
    policy = DecisionEngine(meta)
    payload = search._payload(battle(own, foe), policy.rank_actions(battle(own, foe)), policy)
    for scenario in payload["opponents"]:
        data = scenario[0]["set"]
        assert len(data["moves"]) == 4 and "suckerpunch" in data["moves"]
        assert data["nature"] in {"Adamant", "Jolly"}
        assert sum(data["evs"].values()) <= 510
        assert (data["nature"] == "Adamant") == (data["evs"]["hp"] == 252)


def test_server_disabled_moves_and_choice_lock_remain_legal(search):
    own = pokemon("dragapult", ["shadowball", "dracometeor"], item="choicespecs")
    own.moves["shadowball"]._is_last_used = True
    own._active_turns = 3
    foe = pokemon("blissey", ["splash"])
    arena = battle(own, foe, available_moves=[own.moves["shadowball"]], opponent_can_tera=False)
    policy = engine_for(foe)
    ranked = search.rank(arena, policy.rank_actions(arena), policy)
    assert len(ranked) == 1 and ranked[0].action.id == "shadowball"
    assert ranked[0].diagnostics["search_legal"] == 1


def test_preview_uses_official_first_turn_lead_matrix(search):
    bad = from_export("Excadrill\nAbility: Mold Breaker\nEVs: 252 Atk / 4 Def / 252 Spe\nJolly Nature\n- Earthquake")
    good = from_export("Starmie\nAbility: Natural Cure\nEVs: 252 SpA / 4 SpD / 252 Spe\nTimid Nature\n- Ice Beam\n- Surf")
    foe = from_export("Landorus-Therian\nAbility: Intimidate\nEVs: 252 Atk / 4 Def / 252 Spe\nJolly Nature\n- Earthquake")
    arena = battle(None, None, [bad, good], [foe])
    policy = engine_for(foe)
    order = search.preview_order(arena, policy)
    assert order == [2, 1]
    assert search.last_diagnostics["mode"] == "lead-preview"
    assert search.last_diagnostics["foe_leads"] == 1


def test_preview_preserves_entry_intimidate_and_weather_events(search):
    own = from_export("Pelipper\nAbility: Drizzle\nEVs: 248 HP / 252 Def / 8 SpD\nBold Nature\n- Surf")
    foe = from_export("Landorus-Therian\nAbility: Intimidate\nEVs: 252 Atk / 4 Def / 252 Spe\nJolly Nature\n- Earthquake")
    arena = battle(own, foe)
    policy = engine_for(foe)
    payload = search._payload(arena, policy.rank_actions(arena), policy)
    payload.update(op="inspect", preview=True)
    result = search._rpc(payload)
    assert result["weather"] == "raindance"
    assert result["sides"][0]["pokemon"][0]["boosts"]["atk"] == -1


def test_forced_first_actual_attack_is_evaluated_even_at_minimum_budget():
    dead = pokemon("pikachu", ["thunderbolt"], hp=0)
    dead.status = Status.FNT
    slow = pokemon("heatran", ["flamethrower"])
    fast = pokemon("starmie", ["icebeam"])
    slow._stats["spe"] = 100
    fast._stats["spe"] = 500
    foe = pokemon("garchomp", ["earthquake"], hp=.1)
    foe._stats.update(spe=400, atk=450)
    arena = battle(dead, foe, [slow, fast], force_switch=True, opponent_can_tera=False)
    with SimulatorSearch(time_budget_ms=200, depth=1) as worker:
        assert choose(worker, arena).action is fast


def test_double_knockout_does_not_count_already_fainted_opponent_as_new_ko():
    dead = pokemon("pikachu", ["thunderbolt"], hp=0)
    dead.status = Status.FNT
    foe_dead = pokemon("entei", ["sacredfire"], hp=0)
    foe_dead.status = Status.FNT
    own = pokemon("blissey", ["seismictoss"], stats={"hp": 650, "atk": 50, "def": 130, "spa": 100, "spd": 300, "spe": 100})
    foe = pokemon("blissey", ["seismictoss"], stats={"hp": 650, "atk": 50, "def": 130, "spa": 100, "spd": 300, "spe": 110})
    arena = battle(dead, foe_dead, [own], [foe], force_switch=True, opponent_can_tera=False)
    with SimulatorSearch(time_budget_ms=200, depth=1) as worker:
        chosen = choose(worker, arena)
        assert chosen.diagnostics["ko_chance"] == 0
        assert chosen.diagnostics["expected_received"] > 0
        assert chosen.diagnostics["expected_dealt"] > 0
