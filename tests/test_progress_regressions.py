"""Progress regressions anchored in the Kingambit/Skarmory phazing loop.

Fixtures contain battle facts, never login data. Recorded scores expose the
policy error independently of the simulator so future search changes cannot
silently remove the protection against a repeated observed failure.
"""
from __future__ import annotations

import copy

from poke_env.battle import Effect, Field, Move, Pokemon, PokemonType, SideCondition, Status, Weather

from unaware.engine import Candidate
from unaware.progress import feedback_for_search, progress_context, rerank_for_progress, transition_evidence
from unaware.telemetry import snapshot
from test_decision_regressions import battle, pokemon


def battle_from_saved_snapshot(state):
    """Rehydrate observed state, keeping unknown opponent stats unknown.

    Opponent HP in the public protocol is a percentage, not 100 actual HP.
    This helper intentionally leaves stats/EVs/nature unset for unknown sets.
    Active-turn counters absent in old snapshots remain explicit estimates.
    """
    def mon(data):
        result = Pokemon(9, species=data["species"])
        result._level = int(data.get("level", 100))
        result._stats = {key: int(value) if value is not None else None
                         for key, value in data.get("stats", result.stats).items()}
        result._max_hp = result.stats.get("hp") or 100
        result._current_hp = round(result.max_hp * data.get("hp", 1))
        result._status = Status[data["status"]] if data.get("status") else None
        result._ability, result._item = data.get("ability") or None, data.get("item") or ""
        result.boosts.update({key: int(value) for key, value in data.get("boosts", {}).items()})
        result.moves.update({key: Move(key, gen=9) for key in data.get("moves", {})})
        for key, value in data.get("moves", {}).items():
            result.moves[key]._current_pp = int(value.get("pp", result.moves[key].current_pp))
        result._effects = {Effect[key]: int(value) for key, value in data.get("effects", {}).items()
                           if key in Effect.__members__}
        result._active_turns = int(data.get("active_turns", 1))
        if data.get("tera_type"):
            result._terastallized_type = PokemonType[data["tera_type"]]
        result._terastallized = bool(data.get("terastallized", False))
        types = [PokemonType[key] for key in data.get("types", [])]
        if types and not result._terastallized:
            result._temporary_types = types
        return result
    own = {key: mon(data) for key, data in state["own_roster"].items()}
    foes = {key: mon(data) for key, data in state["foe_roster"].items()}
    own_active, foe_active = own[state["active_id"]], foes[state["foe_id"]]
    def named(values, cls):
        return {cls[key]: int(value) for key, value in values.items() if key in cls.__members__}
    result = battle(own_active, foe_active, [value for value in own.values() if value is not own_active and not value.fainted],
                    team=own, opponent_team=foes, turn=int(state["turn"]),
                    side_conditions=named(state.get("own_hazards", {}), SideCondition),
                    opponent_side_conditions=named(state.get("foe_hazards", {}), SideCondition),
                    weather={Weather[key]: int(state["turn"]) for key in state.get("weather", [])},
                    fields={Field[key]: int(state["turn"]) for key in state.get("fields", [])},
                    force_switch=bool(state.get("force_switch")), can_tera=bool(state.get("can_tera")),
                    finished=bool(state.get("finished")))
    result.teampreview_opponent_team = [Pokemon(9, species=value) for value in state.get("preview_foes", [])]
    return result


def kingambit_skarmory(turn=22, own_hp=244 / 394, foe_hp=1.0):
    """T22: two own survivors, four fainted allies, boosts erased by Whirlwind."""
    own = pokemon("kingambit", ("swordsdance", "kowtowcleave", "suckerpunch", "ironhead"),
                  [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord", "blackglasses", own_hp)
    own._stats.update(hp=394, atk=405, **{"def": 276}, spa=140, spd=206, spe=147)
    own._max_hp, own._current_hp = 394, round(394 * own_hp)
    tusk = pokemon("greattusk", ("earthquake", "knockoff", "rapidspin", "stealthrock"),
                   [252, 0, 252, 0, 4, 0], "impish", "protosynthesis", "heavydutyboots", 290 / 434)
    foe = Pokemon(9, species="skarmory")
    foe.moves.update({key: Move(key, gen=9) for key in ("stealthrock", "whirlwind", "bravebird", "roost")})
    foe._max_hp, foe._current_hp = 100, round(100 * foe_hp)
    dead = [Pokemon(9, species=species) for species in ("gholdengo", "dragapult", "corviknight", "ironvaliant")]
    for mon in dead:
        mon._max_hp, mon._current_hp, mon._status = 100, 0, Status.FNT
    state = battle(own, foe, [tusk], turn=turn,
                   team={"p1: Kingambit": own, "p1: Great Tusk": tusk,
                         **{f"p1: {mon.species}": mon for mon in dead}},
                   opponent_team={"p2: Skarmory": foe},
                   side_conditions={SideCondition.STEALTH_ROCK: 8},
                   opponent_side_conditions={SideCondition.STEALTH_ROCK: 7})
    return state


def decision(before, after, action, *, own_move=None, foe_move=None, switched=False,
             immune=False, failed=False, reliable=True):
    observations = {"resolved_action": True}
    if own_move:
        observations["last_move_own"] = own_move
    if foe_move:
        observations["last_move_foe"] = foe_move
    if switched:
        observations["own_switched"] = True
    if immune:
        observations.update(immune=True, own_immune=True)
    if failed:
        observations.update(failed=True, own_failed=True)
    return {"turn": before["turn"], "before": copy.deepcopy(before), "after": copy.deepcopy(after),
            "action_key": action, "chosen": action, "features": {}, "diagnostics": {},
            "observations": observations,
            "local_outcome": {"reliable": reliable, "dealt": max(0, before["foe_hp_total"] - after["foe_hp_total"]),
                              "received": max(0, before["own_hp_total"] - after["own_hp_total"]),
                              "direct_dealt": 0, "own_ko": False, "foe_ko": False}}


def phaze_history():
    """Actual T20→21→22 cycle: SD erased, 6.1% SR cost, foe heals to full."""
    initial = kingambit_skarmory(turn=20, own_hp=268 / 394, foe_hp=.71)
    before = snapshot(initial)
    initial.active_pokemon = initial.team["p1: Great Tusk"]
    initial.turn = 21
    after = snapshot(initial)
    phazed = decision(before, after, "move:swordsdance", own_move="swordsdance", foe_move="whirlwind", switched=True)
    before_return = snapshot(initial)
    initial.active_pokemon = initial.team["p1: Kingambit"]
    initial.active_pokemon._current_hp = 244
    initial.opponent_active_pokemon._current_hp = 100
    initial.turn = 22
    after_return = snapshot(initial)
    returned = decision(before_return, after_return, "switch:kingambit", foe_move="roost", switched=True)
    return [phazed, returned], initial


def candidate(move, score, *, diagnostics=None, features=None):
    return Candidate(Move(move, gen=9), score=score, label=move,
                     diagnostics=diagnostics or {}, features=features or {})


def test_real_t22_fixture_preserves_unknown_foe_stats_and_actual_hp():
    state = kingambit_skarmory()
    restored = battle_from_saved_snapshot(snapshot(state))
    assert restored.active_pokemon.current_hp == 244
    assert restored.active_pokemon.stats["atk"] == 405
    assert restored.opponent_active_pokemon.stats["hp"] is None
    assert len(restored.team) == 6 and sum(mon.fainted for mon in restored.team.values()) == 4


def test_actual_swords_dance_whirlwind_transition_produces_evidence_without_setup_feature():
    history, _ = phaze_history()
    original = copy.deepcopy(history)
    evidence = transition_evidence(history[0], [])
    assert any(item["action_key"] == "move:swordsdance" and item.get("counter_move") == "whirlwind"
               and item.get("proof") and item.get("reason") for item in evidence)
    assert history == original


def test_observed_phazing_breaks_actual_t22_setup_loop():
    history, state = phaze_history()
    # These are the report's T22 simulator scores. Setup feature was zero.
    options = [candidate("swordsdance", .01), candidate("kowtowcleave", -7.0),
               candidate("suckerpunch", -10.77), candidate("ironhead", -13.35)]
    ranked = rerank_for_progress(snapshot(state), options, history)
    assert ranked[0].action.id != "swordsdance"
    assert {id(option) for option in ranked} == {id(option) for option in options}


def test_capped_swords_dance_loses_to_meaningful_attack():
    state = kingambit_skarmory()
    state.active_pokemon.boosts["atk"] = 6
    options = [candidate("swordsdance", 4), candidate("kowtowcleave", 3)]
    assert rerank_for_progress(snapshot(state), options, [])[0].action.id == "kowtowcleave"


def test_calm_mind_can_still_raise_uncapped_special_defense():
    own = pokemon("mew", ("calmmind", "psychic"), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foe = pokemon("blissey", ("seismictoss",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    own.boosts.update(spa=6, spd=0)
    options = [candidate("calmmind", 20), candidate("psychic", 10)]
    assert rerank_for_progress(snapshot(battle(own, foe)), options, [])[0].action.id == "calmmind"


def test_failed_full_hp_recover_loop_loses_to_progressing_attack():
    own = pokemon("mew", ("recover", "psychic"), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state, history = battle(own, foe), []
    for turn in range(9, 12):
        state.turn = turn
        before = snapshot(state)
        state.turn += 1
        history.append(decision(before, snapshot(state), "move:recover", own_move="recover", failed=True))
    options = [candidate("recover", 8), candidate("psychic", 7)]
    assert rerank_for_progress(snapshot(state), options, history)[0].action.id == "psychic"


def test_known_ground_immunity_never_beats_attack_that_can_connect():
    own = kingambit_skarmory().team["p1: Great Tusk"]
    foe = Pokemon(9, species="skarmory")
    foe._max_hp, foe._current_hp = 100, 20
    state = battle(own, foe)
    before = snapshot(state)
    state.turn += 1
    history = [decision(before, snapshot(state), "move:earthquake", own_move="earthquake", immune=True)]
    options = [candidate("earthquake", 2, diagnostics={"damage_low": 0, "damage_high": 0}),
               candidate("knockoff", 1, diagnostics={"expected_dealt": .18})]
    assert rerank_for_progress(snapshot(state), options, history)[0].action.id == "knockoff"


def test_full_turn_roost_can_make_ground_attack_meaningful():
    """A previous immunity is not a permanent ban: Roost temporarily removes Flying."""
    own = kingambit_skarmory().team["p1: Great Tusk"]
    foe = Pokemon(9, species="skarmory")
    foe._max_hp, foe._current_hp = 100, 20
    state = battle(own, foe)
    before = snapshot(state)
    state.turn += 1
    history = [decision(before, snapshot(state), "move:earthquake", own_move="earthquake", immune=True)]
    # The current simulator certifies a damaging line after faster Roost.
    options = [candidate("earthquake", 100, diagnostics={"expected_dealt": .35, "ko_chance": 1,
                                                        "search_worst": 5000}),
               candidate("knockoff", 1)]
    assert rerank_for_progress(snapshot(state), options, history)[0].action.id == "earthquake"


def test_forced_or_only_legal_choice_is_preserved_after_identical_failures():
    history, state = phaze_history()
    forced = snapshot(state)
    forced["force_switch"] = True
    switch = Candidate(state.team["p1: Great Tusk"], score=1, label="switch")
    assert rerank_for_progress(forced, [switch], history)[0] is switch
    only = candidate("swordsdance", .01)
    assert rerank_for_progress(snapshot(state), [only], history)[0] is only


def test_hazard_removal_and_poison_stalling_are_progress_not_a_loop():
    state = kingambit_skarmory()
    state.active_pokemon = state.team["p1: Great Tusk"]
    before = snapshot(state)
    state.side_conditions.clear()
    state.active_pokemon.boosts["spe"] = 1
    state.turn += 1
    spin = decision(before, snapshot(state), "move:rapidspin", own_move="rapidspin")
    assert not transition_evidence(spin, [])
    own = pokemon("gliscor", ("protect", "toxic", "earthquake"), [244, 0, 252, 0, 12, 0], "impish", "poisonheal")
    foe = pokemon("blissey", ("seismictoss",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure", hp=.9)
    foe.status = Status.TOX
    current, history = battle(own, foe), []
    for turn in range(9, 12):
        current.turn = turn
        before = snapshot(current)
        foe._current_hp -= round(foe.max_hp * .1)
        current.turn += 1
        history.append(decision(before, snapshot(current), "move:protect", own_move="protect", foe_move="seismictoss"))
    assert all(not transition_evidence(item, history[:index]) for index, item in enumerate(history))
    options = [candidate("protect", 10), candidate("earthquake", 9)]
    assert rerank_for_progress(snapshot(current), options, history)[0].action.id == "protect"


def test_repeated_damage_that_lowers_foe_hp_is_not_penalized():
    state = kingambit_skarmory(foe_hp=.9)
    history = []
    for turn in range(22, 25):
        state.turn = turn
        before = snapshot(state)
        state.opponent_active_pokemon._current_hp -= 20
        state.turn += 1
        history.append(decision(before, snapshot(state), "move:kowtowcleave", own_move="kowtowcleave"))
    assert all(not transition_evidence(item, history[:index]) for index, item in enumerate(history))
    options = [candidate("kowtowcleave", 10), candidate("swordsdance", 1)]
    assert rerank_for_progress(snapshot(state), options, history)[0].action.id == "kowtowcleave"


def test_progress_context_ignores_pp_and_absolute_hazard_start_but_keeps_relevant_changes():
    state = snapshot(kingambit_skarmory())
    first = progress_context(state, "setup_countered", "move:swordsdance", "whirlwind")
    repeated = copy.deepcopy(state)
    repeated["turn"] += 10
    repeated["own_hazards"]["STEALTH_ROCK"] += 10
    repeated["foe_hazards"]["STEALTH_ROCK"] += 10
    repeated["own_roster"][repeated["active_id"]]["moves"]["swordsdance"]["pp"] -= 1
    repeated["active_hp"] -= .1
    assert progress_context(repeated, "setup_countered", "move:swordsdance", "whirlwind") == first
    repeated["foe_roster"][repeated["foe_id"]]["status"] = "SLP"
    repeated["foe_status"] = "SLP"
    assert progress_context(repeated, "setup_countered", "move:swordsdance", "whirlwind") != first


def test_different_opponent_does_not_inherit_a_skarmory_phazing_penalty():
    history, state = phaze_history()
    foe = pokemon("blissey", ("seismictoss",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state.opponent_active_pokemon = foe
    state.opponent_team = {"p2: Blissey": foe}
    options = [candidate("swordsdance", 10), candidate("kowtowcleave", 1)]
    assert rerank_for_progress(snapshot(state), options, history)[0].action.id == "swordsdance"


def repeated_switch_cycle():
    """Great Tusk→Kingambit→Whirlwind→Great Tusk, paying SR with no foe damage."""
    state = kingambit_skarmory(turn=23, own_hp=.62, foe_hp=1)
    king, tusk = state.team["p1: Kingambit"], state.team["p1: Great Tusk"]
    state.active_pokemon = tusk
    history = []
    for _ in range(3):
        before = snapshot(state)
        state.active_pokemon = king
        king._current_hp -= 24
        state.turn += 1
        history.append(decision(before, snapshot(state), "switch:kingambit", switched=True, foe_move="roost"))
        before = snapshot(state)
        state.active_pokemon = tusk
        state.turn += 1
        history.append(decision(before, snapshot(state), "move:swordsdance", own_move="swordsdance",
                                switched=True, foe_move="whirlwind"))
    return history, state


def test_repeated_costly_return_switch_loses_to_safe_hazard_removal():
    history, state = repeated_switch_cycle()
    last = transition_evidence(history[-1], history[:-1])
    assert any(item["kind"] == "damaging_cycle" and item["proof"]["own_hp_cost"] > .035 for item in last)
    switch = Candidate(state.team["p1: Kingambit"], score=8, label="Switch Kingambit")
    spin = candidate("rapidspin", 7, features={"removal": .1}, diagnostics={"faint_risk": 0})
    assert rerank_for_progress(snapshot(state), [switch, spin], history)[0] is spin


def test_useful_recovery_surviving_a_cycle_is_preserved():
    history, state = repeated_switch_cycle()
    recover = candidate("recover", 8, features={"heal": .5},
                        diagnostics={"faint_risk": 0, "expected_received": .4})
    # Reuse a recurrent action key, while the current line has useful recovery.
    for item in history:
        item["action_key"] = "move:recover"
    attack = candidate("psychic", 7, diagnostics={"faint_risk": 1})
    assert rerank_for_progress(snapshot(state), [recover, attack], history)[0] is recover


def test_certified_terminal_win_dominates_old_counter_evidence():
    history, state = phaze_history()
    guaranteed = candidate("swordsdance", 5001, diagnostics={"search_worst": 5000, "ko_chance": 1})
    alternative = candidate("kowtowcleave", 1)
    assert rerank_for_progress(snapshot(state), [guaranteed, alternative], history)[0] is guaranteed
    assert guaranteed.score == 5001


def test_no_damage_feedback_transports_through_a_new_screen():
    """T11 Ice Beam damage is evidence; a new Light Screen changes its equation."""
    own = pokemon("dragapult", ("shadowball",), [0, 0, 4, 252, 0, 252], "timid",
                  "infiltrator", "choicespecs")
    foe = pokemon("lanturn", ("icebeam",), [252, 0, 0, 252, 4, 0], "modest", "voltabsorb", hp=.88)
    state = battle(own, foe, turn=11)
    before = snapshot(state)
    own._current_hp = round(own.max_hp * .3)
    foe._current_hp = round(foe.max_hp * .56)
    state.turn = 12
    trace = decision(before, snapshot(state), "move:shadowball", own_move="shadowball", foe_move="icebeam")
    trace["local_outcome"].update(direct_received=.7, direct_dealt=.32)
    assert any(row["attacker"] == "foe" for row in feedback_for_search(snapshot(state), [trace])["observed_damage"])
    state.side_conditions[SideCondition.LIGHT_SCREEN] = 11
    current = feedback_for_search(snapshot(state), [trace])
    assert not any(row["attacker"] == "foe" for row in current["observed_damage"]), \
        "Old damage cannot calibrate current hidden sets after a new damage modifier."


def test_reranking_the_same_request_cannot_accumulate_a_penalty_twice():
    history, state = phaze_history()
    options = [candidate("swordsdance", .01), candidate("kowtowcleave", -7)]
    ranked = rerank_for_progress(snapshot(state), options, history)
    once = {option.action.id: option.score for option in ranked}
    twice = rerank_for_progress(snapshot(state), ranked, history)
    assert {option.action.id: option.score for option in twice} == once
