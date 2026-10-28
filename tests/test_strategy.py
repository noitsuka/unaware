"""Export-build classification and strategic priors subordinate to tactics."""
import copy

import pytest
from poke_env.battle import Pokemon, SideCondition, Status

from unaware.engine import Candidate
from unaware.strategy import analyze_opponent, analyze_team, strategy_action_adjustments
from unaware.teams import TeamMember, parse_team
from test_decision_regressions import battle, pokemon


def member(species, moves, *, item="leftovers", ability="", offensive=False, speedy=False, defensive=False):
    result = TeamMember(species=species, moves=list(moves), item=item, ability=ability)
    result.evs = {"hp": 252 if defensive else 0, "atk": 252 if offensive else 0,
                  "def": 252 if defensive else 0, "spa": 0, "spd": 4, "spe": 252 if speedy else 0}
    result.nature = "Adamant" if offensive else "Impish" if defensive else "Serious"
    return result


def hyper_offense_team():
    return [
        member("Glimmora", ("stealthrock", "spikes", "mortalspin", "memento"), item="focussash", ability="toxicdebris", speedy=True),
        member("Dragonite", ("dragondance", "extremespeed", "earthquake", "icespinner"), item="lumberry", offensive=True, speedy=True),
        member("Kingambit", ("swordsdance", "suckerpunch", "kowtowcleave", "ironhead"), item="blackglasses", offensive=True),
        member("Garchomp", ("swordsdance", "earthquake", "stoneedge", "dragonclaw"), item="lifeorb", offensive=True, speedy=True),
        member("Great Tusk", ("bulkup", "headlongrush", "closecombat", "icespinner"), item="boosterenergy", offensive=True, speedy=True),
        member("Mimikyu", ("swordsdance", "playrough", "shadowclaw", "shadowsneak"), item="lifeorb", offensive=True, speedy=True),
    ]


def stall_team():
    return [
        member("Blissey", ("softboiled", "seismictoss", "toxic", "protect"), defensive=True),
        member("Dondozo", ("rest", "sleeptalk", "bodypress", "curse"), defensive=True),
        member("Clodsire", ("recover", "earthquake", "toxic", "spikes"), defensive=True),
        member("Alomomola", ("wish", "protect", "toxic", "flipturn"), defensive=True, ability="regenerator"),
        member("Toxapex", ("recover", "toxic", "haze", "scald"), defensive=True, ability="regenerator"),
        member("Skarmory", ("roost", "whirlwind", "spikes", "bodypress"), defensive=True),
    ]


def roles_for(profile, species):
    return next(set(row["roles"]) for row in profile["roles"] if row["species"] == species)


def adjustments(state, options, profile, opponent=None):
    return {row["action_key"]: row for row in strategy_action_adjustments(state, options, profile, opponent)}


def test_support_lead_plus_multiple_setup_attackers_is_hyper_offense():
    profile = analyze_team(hyper_offense_team())
    assert profile["archetype"] == "hyper_offense"
    assert profile["lead_species"] == "glimmora"
    assert "suicide_lead" in roles_for(profile, "glimmora")
    assert "kingambit" in profile["win_conditions"]
    assert profile["confidence"] >= .8


def test_sticky_web_support_counts_as_a_plan_even_without_focus_sash():
    team = hyper_offense_team()
    team[0] = member("Ribombee", ("stickyweb", "moonblast", "stunspore", "uturn"), item="heavydutyboots", speedy=True)
    profile = analyze_team(team)
    assert profile["archetype"] == "hyper_offense"
    assert profile["lead_species"] == "ribombee"
    assert "web_setter" in roles_for(profile, "ribombee")


def test_walls_recovery_and_status_classify_as_stall():
    profile = analyze_team(stall_team())
    assert profile["archetype"] == "stall"
    assert "blissey" in profile["win_conditions"]
    assert all("wall" in row["roles"] for row in profile["roles"])


def test_five_walls_plus_a_setup_cleaner_classify_as_semi_stall():
    team = stall_team()
    team[-1] = hyper_offense_team()[2]
    profile = analyze_team(team)
    assert profile["archetype"] == "semi_stall"
    assert "kingambit" in profile["win_conditions"]


def test_realistic_ou_bulk_and_pivots_classify_as_bulky_offense():
    export = """Great Tusk @ Heavy-Duty Boots
Ability: Protosynthesis
EVs: 252 HP / 252 Def / 4 SpD
Impish Nature
- Earthquake
- Knock Off
- Rapid Spin
- Stealth Rock

Gholdengo @ Air Balloon
Ability: Good as Gold
EVs: 252 SpA / 4 SpD / 252 Spe
Timid Nature
- Nasty Plot
- Make It Rain
- Shadow Ball
- Recover

Dragapult @ Choice Specs
Ability: Infiltrator
EVs: 252 SpA / 4 SpD / 252 Spe
Timid Nature
- Draco Meteor
- Shadow Ball
- U-turn
- Flamethrower

Corviknight @ Rocky Helmet
Ability: Pressure
EVs: 248 HP / 252 Def / 8 SpD
Impish Nature
- Brave Bird
- U-turn
- Roost
- Taunt

Kingambit @ Black Glasses
Ability: Supreme Overlord
EVs: 252 HP / 252 Atk / 4 SpD
Adamant Nature
- Swords Dance
- Kowtow Cleave
- Sucker Punch
- Iron Head

Iron Valiant @ Booster Energy
Ability: Quark Drive
EVs: 252 SpA / 4 SpD / 252 Spe
Naive Nature
- Moonblast
- Close Combat
- Knock Off
- Encore
"""
    profile = analyze_team(parse_team(export))
    assert profile["archetype"] == "bulky_offense"
    assert "pivot" in roles_for(profile, "dragapult")
    assert "wall" in roles_for(profile, "corviknight")
    assert {"gholdengo", "kingambit"} <= set(profile["win_conditions"])


def test_same_species_changes_role_with_moves_evs_and_item():
    tank = member("Garchomp", ("stealthrock", "earthquake", "dragontail", "protect"), item="rockyhelmet", defensive=True)
    sweeper = member("Garchomp", ("swordsdance", "earthquake", "stoneedge", "dragonclaw"), item="lifeorb", offensive=True, speedy=True)
    tank_profile, offense_profile = analyze_team([tank]), analyze_team([sweeper])
    assert "wall" in roles_for(tank_profile, "garchomp")
    assert "wall" not in roles_for(offense_profile, "garchomp")
    assert "setup_sweeper" in roles_for(offense_profile, "garchomp")
    assert offense_profile["roles"][0]["offense_score"] > tank_profile["roles"][0]["offense_score"]


def test_focus_sash_attacker_without_support_does_not_become_a_suicide_lead():
    attacker = member("Mimikyu", ("swordsdance", "playrough", "shadowclaw", "shadowsneak"),
                      item="focussash", offensive=True, speedy=True)
    assert "suicide_lead" not in roles_for(analyze_team([attacker]), "mimikyu")


def test_manual_style_changes_plan_but_keeps_build_roles_and_inference():
    original = hyper_offense_team()
    preserved = copy.deepcopy(original)
    automatic, manual = analyze_team(original), analyze_team(original, "balance")
    assert manual["archetype"] == "balance" and manual["inferred_archetype"] == "hyper_offense"
    assert manual["roles"] == automatic["roles"]
    assert original == preserved
    with pytest.raises(ValueError):
        analyze_team(original, "made-up-style")


def test_empty_or_unknown_team_has_no_confident_fake_plan():
    assert analyze_team([])["confidence"] == 0
    unknown = analyze_team([TeamMember("Not A Pokemon", moves=["tackle"])])
    assert unknown["confidence"] == 0 and unknown["lead_species"] == ""


def test_ho_setup_reward_is_withheld_against_revealed_whirlwind():
    team = hyper_offense_team()
    own = pokemon("kingambit", ("swordsdance", "kowtowcleave"), [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord")
    foe = pokemon("skarmory", ("whirlwind", "roost", "bravebird"), [252, 0, 252, 0, 4, 0], "impish", "sturdy")
    options = [Candidate(own.moves["swordsdance"], score=1), Candidate(own.moves["kowtowcleave"], score=0)]
    reserve = pokemon("greattusk", ("earthquake",), [252, 0, 252, 0, 4, 0], "impish", "protosynthesis")
    result = adjustments(battle(own, foe, [reserve]), options, analyze_team(team))
    assert result["move:swordsdance"]["adjustment"] == 0


def test_stall_protect_with_poison_progress_is_supported_without_forcing_repetition():
    own = pokemon("blissey", ("protect", "seismictoss"), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    foe = pokemon("mew", ("psychic",), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foe.status = Status.TOX
    options = [Candidate(own.moves["protect"], score=1), Candidate(own.moves["seismictoss"], score=0)]
    state, profile = battle(own, foe), analyze_team(stall_team())
    assert adjustments(state, options, profile)["move:protect"]["adjustment"] > 0
    own._protect_counter = 1
    assert adjustments(state, options, profile)["move:protect"]["adjustment"] == 0


def test_decisive_ko_forced_choice_or_terminal_win_cannot_be_overridden_by_style():
    own = pokemon("kingambit", ("swordsdance", "kowtowcleave"), [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord")
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state, profile = battle(own, foe), analyze_team(hyper_offense_team())
    options = [Candidate(own.moves["swordsdance"], score=9),
               Candidate(own.moves["kowtowcleave"], score=10, diagnostics={"ko_chance": 1, "faint_risk": 0})]
    before = [option.score for option in options]
    assert all(row["adjustment"] == 0 for row in strategy_action_adjustments(state, options, profile))
    assert [option.score for option in options] == before
    options[1].diagnostics = {"search_worst": 5000}
    assert all(row["adjustment"] == 0 for row in strategy_action_adjustments(state, options, profile))
    state.force_switch = True
    assert all(row["adjustment"] == 0 for row in strategy_action_adjustments(state, options, profile))


def test_existing_hazards_and_grounded_ghost_spinner_get_no_false_progress_bonus():
    own = pokemon("greattusk", ("rapidspin", "stealthrock"), [252, 0, 252, 0, 4, 0], "impish", "protosynthesis")
    foe = pokemon("gholdengo", ("shadowball",), [0, 0, 0, 252, 4, 252], "timid", "goodasgold")
    state = battle(own, foe, side_conditions={SideCondition.SPIKES: 1},
                   opponent_side_conditions={SideCondition.STEALTH_ROCK: 5})
    state.teampreview_opponent_team = [Pokemon(9, species=species) for species in ("gholdengo", "mew", "blissey")]
    options = [Candidate(move, score=1) for move in own.moves.values()]
    results = adjustments(state, options, analyze_team(hyper_offense_team()))
    assert results["move:rapidspin"]["adjustment"] == 0
    assert results["move:stealthrock"]["adjustment"] == 0


def test_strategy_priors_are_bounded_explained_and_do_not_mutate_candidates():
    own = pokemon("dragonite", ("dragondance", "uturn"), [0, 252, 4, 0, 0, 252], "adamant", "multiscale")
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    options = [Candidate(move, score=10) for move in own.moves.values()]
    result = strategy_action_adjustments(battle(own, foe), options, analyze_team(hyper_offense_team()))
    assert all(abs(row["adjustment"]) <= 3 for row in result)
    assert all(row["reasons"] for row in result if row["adjustment"])
    assert [option.score for option in options] == [10, 10]


def test_own_screens_are_not_hazards_to_remove_and_are_not_reapplied():
    own = pokemon("mew", ("reflect", "defog", "rapidspin"), [252, 0, 0, 0, 4, 252], "timid", "synchronize")
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state = battle(own, foe, side_conditions={SideCondition.REFLECT: 4})
    results = adjustments(state, [Candidate(move, score=0) for move in own.moves.values()], analyze_team(hyper_offense_team()))
    assert all(row["adjustment"] == 0 for row in results.values())


def test_hazard_plan_counts_observed_faints_over_stale_healthy_preview():
    own = pokemon("glimmora", ("stealthrock", "memento"), [0, 0, 0, 252, 4, 252], "timid", "toxicdebris")
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    dead = [pokemon(species, ("tackle",), [252, 0, 0, 0, 4, 0], "serious", "", hp=0)
            for species in ("mew", "garchomp")]
    state = battle(own, foe)
    state.opponent_team.update({f"p2: {index}": mon for index, mon in enumerate(dead)})
    state.teampreview_opponent_team = [Pokemon(9, species=species) for species in ("blissey", "mew", "garchomp")]
    options = [Candidate(move, score=0) for move in own.moves.values()]
    assert adjustments(state, options, analyze_team(hyper_offense_team()))["move:stealthrock"]["adjustment"] == 0


def test_mortal_spin_cannot_remove_hazards_through_a_steel_immunity():
    own = pokemon("glimmora", ("mortalspin", "powergem"), [0, 0, 0, 252, 4, 252], "timid", "toxicdebris")
    foe = pokemon("skarmory", ("roost",), [252, 0, 252, 0, 4, 0], "impish", "sturdy")
    state = battle(own, foe, side_conditions={SideCondition.STEALTH_ROCK: 5})
    options = [Candidate(move, score=0) for move in own.moves.values()]
    assert adjustments(state, options, analyze_team(hyper_offense_team()))["move:mortalspin"]["adjustment"] == 0


@pytest.mark.parametrize("forced,should_adjust", [([False], True), ([True], False)])
def test_list_form_forced_switch_matches_actual_server_flag(forced, should_adjust):
    own = pokemon("kingambit", ("swordsdance", "kowtowcleave"), [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord")
    foe = pokemon("blissey", ("softboiled",), [252, 0, 252, 0, 4, 0], "bold", "naturalcure")
    state = battle(own, foe, force_switch=forced)
    options = [Candidate(move, score=0) for move in own.moves.values()]
    bonus = adjustments(state, options, analyze_team(hyper_offense_team()))["move:swordsdance"]["adjustment"]
    assert (bonus > 0) is should_adjust


def test_opponent_preview_alone_does_not_reveal_species_sets_or_confident_roles():
    own = pokemon("mew", ("psychic",), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foe = Pokemon(9, species="blissey")
    state = battle(own, foe)
    state.teampreview_opponent_team = [Pokemon(9, species=species) for species in ("blissey", "dondozo", "clodsire")]
    profile = analyze_opponent(state)
    assert profile["confidence"] == 0
    assert profile["incomplete"] is True and profile["assumptions"]
    assert all(row["roles"] == ["unknown"] for row in profile["roles"])
    assert all(row["item"] == "" and row["ability"] == "" for row in profile["roles"])


def test_observed_opponent_defensive_build_is_provisional_and_ignores_hidden_stats():
    own = pokemon("mew", ("psychic",), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foes = [Pokemon(9, species=row.species) for row in stall_team()]
    for mon, exported in zip(foes, stall_team()):
        from poke_env.battle import Move
        mon.moves.update({move: Move(move, gen=9) for move in exported.moves})
        mon._ability = exported.ability or None
    state = battle(own, foes[0])
    state.opponent_team = {f"p2: {i}": mon for i, mon in enumerate(foes)}
    original = analyze_opponent(state)
    assert original["archetype"] == "stall"
    assert original["confidence"] == .9 and original["incomplete"] is True
    for mon in foes:
        mon._stats = dict(hp=999, atk=999, spa=999, spe=999, **{"def": 1, "spd": 1})
        mon._evs, mon._nature = [0, 252, 0, 252, 0, 0], "modest"
    assert analyze_opponent(state) == original
    assert all(not ({"evs", "ivs", "nature", "stats"} & row.keys()) for row in original["roles"])


def test_partially_revealed_moves_reduce_opponent_plan_confidence():
    own = pokemon("mew", ("psychic",), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foe = pokemon("skarmory", ("roost", "whirlwind", "spikes", "bodypress"), [252, 0, 252, 0, 4, 0], "impish", "sturdy")
    state = battle(own, foe)
    full = analyze_opponent(state)
    del foe.moves["whirlwind"]
    del foe.moves["spikes"]
    partial = analyze_opponent(state)
    assert full["revealed_move_fraction"] == 1
    assert partial["revealed_move_fraction"] == .5
    assert partial["confidence"] < full["confidence"]


def test_current_form_and_preview_merge_without_inventing_extra_opponents():
    own = pokemon("mew", ("psychic",), [252, 0, 0, 252, 4, 0], "modest", "synchronize")
    foe = Pokemon(9, species="palafinhero")
    state = battle(own, foe)
    state.teampreview_opponent_team = [Pokemon(9, species="palafin")]
    assert len(analyze_opponent(state)["roles"]) == 1


@pytest.mark.parametrize("counter,can_block", [("whirlwind", False), ("roar", False), ("dragontail", False),
                                             ("circlethrow", False), ("haze", True), ("encore", True)])
def test_last_surviving_win_condition_ignores_failed_phazing_but_respects_boost_denial(counter, can_block):
    own = pokemon("kingambit", ("swordsdance", "kowtowcleave"), [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord")
    foe = pokemon("mew", (counter, "softboiled"), [252, 0, 252, 0, 4, 0], "bold", "synchronize")
    state = battle(own, foe)
    options = [Candidate(move, score=0) for move in own.moves.values()]
    bonus = adjustments(state, options, analyze_team(hyper_offense_team()))["move:swordsdance"]["adjustment"]
    assert (bonus == 0) is can_block


def test_exhausted_haze_no_longer_blocks_a_strategic_setup_opportunity():
    own = pokemon("kingambit", ("swordsdance", "kowtowcleave"), [252, 252, 0, 0, 4, 0], "adamant", "supremeoverlord")
    foe = pokemon("mew", ("haze", "softboiled"), [252, 0, 252, 0, 4, 0], "bold", "synchronize")
    foe.moves["haze"]._current_pp = 0
    options = [Candidate(move, score=0) for move in own.moves.values()]
    assert adjustments(battle(own, foe), options, analyze_team(hyper_offense_team()))["move:swordsdance"]["adjustment"] > 0
