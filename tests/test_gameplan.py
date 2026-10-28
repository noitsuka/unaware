"""Game plan: suicide leads set the field, switches cost tempo, loops break."""
from pathlib import Path
from poke_env.battle import Move, SideCondition

from unaware.engine import Candidate
from unaware.gameplan import GamePlan, field_moves_pending
from unaware.strategy import analyze_team
from test_decision_regressions import battle, pokemon
from test_strategy import hyper_offense_team, stall_team


def glimmora():
    return pokemon("glimmora", ("stealthrock", "spikes", "mortalspin", "powergem"),
                   [0, 0, 0, 252, 4, 252], "timid", "toxicdebris", "focussash")


def dragonite():
    return pokemon("dragonite", ("dragondance", "extremespeed", "earthquake", "icespinner"),
                   [0, 252, 0, 0, 4, 252], "adamant", "multiscale", "lumberry")


def foe():
    return pokemon("gholdengo", ("makeitrain", "shadowball", "nastyplot", "recover"),
                   [0, 0, 0, 252, 4, 252], "timid", "goodasgold", "choicescarf")


def move(name, score, **diag):
    return Candidate(Move(name, gen=9), score=score, label=name,
                     diagnostics={"search_legal": 1.0, **diag})


def switch(mon, score, **diag):
    return Candidate(mon, score=score, label="switch", diagnostics={"search_legal": 1.0, **diag})


def ranked(state, options, profile, history=(), snapshot=None):
    return GamePlan().apply(state, options, profile, {}, list(history), snapshot or {})


def test_suicide_lead_sets_rocks_instead_of_attacking():
    profile = analyze_team(hyper_offense_team())
    own, bench = glimmora(), dragonite()
    state = battle(own, foe(), (bench,))
    options = [move("powergem", 80, expected_dealt=.35, faint_risk=.2),
               move("stealthrock", 25, faint_risk=.2), switch(bench, 30, faint_risk=.1)]
    assert ranked(state, options, profile)[0].label == "stealthrock"


def test_lead_moves_on_to_spikes_then_stops_once_field_is_set():
    own = glimmora()
    state = battle(own, foe(), opponent_side_conditions={SideCondition.STEALTH_ROCK: 1})
    assert field_moves_pending(state, own) == ["spikes"]
    state = battle(own, foe(), opponent_side_conditions={SideCondition.STEALTH_ROCK: 1, SideCondition.SPIKES: 2})
    assert field_moves_pending(state, own) == []


def test_unthreatened_attacker_does_not_switch_out():
    profile = analyze_team(hyper_offense_team())
    own, bench = dragonite(), glimmora()
    state = battle(own, foe(), (bench,))
    options = [move("extremespeed", 40, expected_dealt=.3, faint_risk=.1), switch(bench, 60)]
    assert ranked(state, options, profile)[0].label == "extremespeed"


def test_switch_ping_pong_is_penalised():
    profile = analyze_team(stall_team())
    own, bench = dragonite(), glimmora()
    state = battle(own, foe(), (bench,))
    history = [{"action_key": "switch:dragonite", "before": {"active": "glimmora", "force_switch": False}}]
    options = [move("earthquake", 40, expected_dealt=.1, faint_risk=.6), switch(bench, 70)]
    result = ranked(state, options, profile, history)
    assert result[0].label == "earthquake"


def test_repeated_play_that_loses_hp_is_broken():
    profile = analyze_team(stall_team())
    own = dragonite()
    state = battle(own, foe())
    lost = {"action_key": "move:icespinner", "before": {"active": "dragonite", "foe_active": "gholdengo"},
            "local_outcome": {"reliable": True, "direct_dealt": 0., "direct_received": .25}}
    options = [move("icespinner", 50, expected_dealt=.05), move("earthquake", 40, expected_dealt=.3)]
    assert ranked(state, options, profile, [lost, lost])[0].label == "earthquake"


def test_decisive_results_are_never_reordered():
    profile = analyze_team(hyper_offense_team())
    own = glimmora()
    state = battle(own, foe())
    options = [move("powergem", 10000), move("stealthrock", 25)]
    assert ranked(state, options, profile)[0].label == "powergem"


def test_tera_is_not_spent_on_hazards_without_a_real_gain():
    profile = analyze_team(hyper_offense_team())
    state = battle(glimmora(), foe())
    tera = move("stealthrock", 91, faint_risk=.2)
    tera.terastallize = True
    options = [tera, move("stealthrock", 75, faint_risk=.2)]
    best = ranked(state, options, profile)[0]
    assert best.label == "stealthrock" and not best.terastallize


def test_attacking_a_wall_that_recovers_everything_is_a_stalemate():
    profile = analyze_team(hyper_offense_team())
    own, target = dragonite(), foe()
    state = battle(own, target)
    hit = {"action_key": "move:earthquake", "before": {"active": "dragonite", "foe_active": "gholdengo", "foe_hp": 1.0},
           "local_outcome": {"reliable": True, "direct_dealt": .3, "direct_received": 0.}}
    options = [move("earthquake", 60, expected_dealt=.3), move("dragondance", 30)]
    assert ranked(state, options, profile, [hit, hit, hit])[0].label == "dragondance"


def test_choice_lock_is_inferred_from_a_repeated_attack():
    from unaware.gameplan import inferred_choice_lock
    from poke_env.battle import Pokemon
    foe_mon = Pokemon(9, species="gholdengo")
    row = {"before": {"foe_active": "gholdengo"}, "observations": {"last_move_foe": "shadowball"}}
    assert inferred_choice_lock([row, row], foe_mon) == "shadowball"
    assert inferred_choice_lock([row], foe_mon) == ""
    switched = {"before": {"foe_active": "gholdengo"}, "observations": {"last_move_foe": "shadowball", "foe_switched": True}}
    assert inferred_choice_lock([row, switched], foe_mon) == ""


def test_no_setup_into_a_likely_unaware_wall():
    from poke_env.battle import Pokemon
    profile = analyze_team(hyper_offense_team())
    dozo = Pokemon(9, species="dondozo")
    state = battle(dragonite(), dozo)
    options = [move("dragondance", 60), move("earthquake", 40, expected_dealt=.2)]
    assert ranked(state, options, profile)[0].label == "earthquake"


def test_heavy_hazards_on_our_side_make_removal_urgent():
    from poke_env.battle import SideCondition
    profile = analyze_team(hyper_offense_team())
    tusk = pokemon("greattusk", ("headlongrush", "closecombat", "icespinner", "rapidspin"),
                   [0, 252, 0, 0, 4, 252], "jolly", "protosynthesis", "boosterenergy")
    bench = [dragonite(), glimmora()]
    state = battle(tusk, foe(), bench, side_conditions={SideCondition.STEALTH_ROCK: 1, SideCondition.SPIKES: 2})
    options = [move("headlongrush", 60, expected_dealt=.3), move("rapidspin", 30, expected_dealt=.08)]
    assert ranked(state, options, profile)[0].label == "rapidspin"


def test_no_recovery_at_full_hp_in_any_style():
    rain = analyze_team(hyper_offense_team())
    rain["archetype"] = "weather"
    pelipper = pokemon("pelipper", ("hurricane", "uturn", "roost", "surf"), [248, 0, 252, 0, 8, 0], "bold", "drizzle", "damprock")
    state = battle(pelipper, foe())
    options = [move("roost", 9), move("uturn", 3, expected_dealt=.1)]
    assert ranked(state, options, rain)[0].label == "uturn"


def test_secured_ko_is_taken_instead_of_switching():
    profile = analyze_team(stall_team())
    own, bench = dragonite(), glimmora()
    state = battle(own, foe(), (bench,))
    options = [move("extremespeed", 40, expected_dealt=.2, ko_chance=.95, faint_risk=.05), switch(bench, 85)]
    assert ranked(state, options, profile)[0].label == "extremespeed"


def lokix():
    return pokemon("lokix", ("leechlife", "knockoff", "suckerpunch", "swordsdance"),
                   [0, 252, 0, 0, 4, 252], "jolly", "tintedlens", "lifeorb")


def tyranitar(hp=.94, status=None):
    from poke_env.battle import Status
    mon = pokemon("tyranitar", ("stoneedge", "knockoff", "icebeam", "thunderwave"),
                  [252, 0, 0, 0, 252, 0], "careful", "sandstream", "leftovers", hp)
    if status:
        mon.status = getattr(Status, status)
    return mon


def test_guaranteed_ko_beats_a_switch_guess():
    """Replay T12: +2 Lokix had a certain Leech Life KO on a frozen Tyranitar."""
    profile = analyze_team(hyper_offense_team())
    state = battle(lokix(), tyranitar(status="FRZ"))
    knock = move("knockoff", 159, expected_dealt=.68, ko_chance=.52, damage_low=.86, accuracy=1.)
    leech = move("leechlife", 135, expected_dealt=.63, ko_chance=.36, damage_low=1.42, accuracy=1.)
    assert ranked(state, [knock, leech], profile)[0].label == "leechlife"


def test_setup_is_safe_against_a_frozen_target():
    profile = analyze_team(hyper_offense_team())
    state = battle(lokix(), tyranitar(status="FRZ"))
    sd = move("swordsdance", 40, faint_risk=.2, search_faint_risk_worst=.5, search_received_worst=.1)
    sd.features["policy_setup"] = .5
    knock = move("knockoff", 45, expected_dealt=.3, damage_low=.2)
    assert ranked(state, [sd, knock], profile)[0].label == "swordsdance"


def test_rain_setter_is_preserved_on_turn_one():
    """Replay vs socaselectah: Pelipper stayed in and died to Thunderbolt on T1."""
    from unaware.teams import parse_team
    rain_text = (Path(__file__).resolve().parents[1] / "tools/library_seed.txt").read_text(encoding="utf-8").split("=== Rain | weather")[1].split("=== ")[0]
    profile = analyze_team(parse_team(rain_text), "weather")
    pelipper = pokemon("pelipper", ("hurricane", "uturn", "roost", "surf"), [248, 0, 252, 0, 8, 0], "bold", "drizzle", "damprock")
    kingambit = pokemon("kingambit", ("swordsdance", "kowtowcleave", "suckerpunch", "ironhead"), [0, 252, 0, 0, 4, 252], "adamant", "supremeoverlord", "leftovers")
    state = battle(pelipper, foe(), (kingambit,))
    options = [move("surf", -5, faint_risk=.9, expected_dealt=.3), move("hurricane", -7, faint_risk=.9), switch(kingambit, -16, faint_risk=.05)]
    assert ranked(state, options, profile)[0].label == "switch"


def test_no_hazard_reset_into_a_revealed_defogger():
    """Replay: Great Tusk kept setting Rocks while Mandibuzz kept Defogging."""
    from poke_env.battle import Move as PMove
    profile = analyze_team(stall_team())
    tusk = pokemon("greattusk", ("headlongrush", "closecombat", "icespinner", "stealthrock"),
                   [0, 252, 0, 0, 4, 252], "jolly", "protosynthesis", "boosterenergy")
    buzz = pokemon("mandibuzz", ("foulplay", "roost", "defog", "toxic"), [252, 0, 252, 0, 4, 0], "impish", "overcoat", "heavydutyboots")
    state = battle(tusk, buzz)
    options = [move("stealthrock", 55), move("icespinner", 30, expected_dealt=.35)]
    assert ranked(state, options, profile)[0].label == "icespinner"


def raging_bolt(hp):
    return pokemon("ragingbolt", ("thunderbolt", "dracometeor", "calmmind", "weatherball"),
                   [0, 0, 4, 252, 0, 252], "modest", "protosynthesis", "boosterenergy", hp)


def test_ko_bonus_needs_a_safe_worst_case_when_moving_second():
    """Replay vs beans727 T8: slower Slowking at 20% got the KO bonus and died first."""
    profile = analyze_team(hyper_offense_team())
    slowking = pokemon("slowkinggalar", ("sludgebomb", "psyshock", "icebeam", "chillyreception"),
                       [252, 0, 4, 252, 0, 0], "modest", "regenerator", "heavydutyboots", .2)
    sludge = move("sludgebomb", -11, damage_low=.26, accuracy=1., faint_risk=.11,
                  policy_first_probability=0., search_faint_risk_worst=1.)
    GamePlan().apply(battle(slowking, raging_bolt(.17)), [sludge, move("icebeam", -14)], profile, {}, [], {})
    assert "plan_adjustment" not in sludge.diagnostics or sludge.diagnostics["plan_adjustment"] < 55


def test_tera_proof_ko_is_preferred_while_the_foe_can_tera():
    """Replay vs beans727 T9: Headlong Rush into Tera Flying; Ice Spinner also KOed."""
    profile = analyze_team(hyper_offense_team())
    tusk = pokemon("greattusk", ("headlongrush", "closecombat", "icespinner", "rapidspin"),
                   [0, 252, 0, 0, 4, 252], "jolly", "protosynthesis", "boosterenergy")
    options = lambda: [move("headlongrush", 26, damage_low=1.41, accuracy=1., policy_first_probability=1.),
                       move("icespinner", 20, damage_low=.63, accuracy=1., policy_first_probability=1.)]
    can_tera = battle(tusk, raging_bolt(.17), opponent_used_tera=False)
    assert ranked(can_tera, options(), profile)[0].label == "icespinner"
    spent = battle(tusk, raging_bolt(.17), opponent_used_tera=True)
    assert ranked(spent, options(), profile)[0].label == "headlongrush"


def test_burned_physical_win_condition_is_not_a_key_piece():
    """Replay vs beans727 T14: burned Zamazenta was 'preserved', starting a switch dance."""
    from poke_env.battle import Status
    profile = analyze_team(hyper_offense_team())
    zama = pokemon("zamazenta", ("closecombat", "stoneedge", "crunch", "icefang"),
                   [0, 252, 4, 0, 0, 252], "jolly", "dauntlessshield", "leftovers", .45)
    profile["win_conditions"] = ["zamazenta"]
    bench = [dragonite(), glimmora(), lokix()]
    options = lambda: [move("closecombat", -3, expected_dealt=.3, faint_risk=.8), switch(bench[0], -10, faint_risk=.0)]
    healthy = ranked(battle(zama, foe(), bench), options(), profile)
    assert any("key piece" in r for c in healthy for r in c.reasons)
    zama.status = Status.BRN
    burned = ranked(battle(zama, foe(), bench), options(), profile)
    assert not any("key piece" in r for c in burned for r in c.reasons)


def test_tempo_penalties_fade_when_staying_in_is_likely_to_faint():
    """Replay 2697886245 T6: Zapdos stayed in (5% average risk, 100% worst, moving
    second) because the switch carried -87 of plan penalties, then died to Moonblast."""
    profile = analyze_team(hyper_offense_team())
    own, bench = dragonite(), glimmora()
    history = [{"action_key": "switch:dragonite", "before": {"active": "kingambit", "force_switch": False}}]
    options = lambda: [move("extremespeed", -57, expected_dealt=.3, faint_risk=.05, search_faint_risk_worst=1.,
                            policy_first_probability=0.),
                       move("earthquake", -100, expected_dealt=.2, faint_risk=.5, search_faint_risk_worst=1.,
                            damage_low=1.8, accuracy=1., policy_first_probability=0.),
                       switch(bench, -40, faint_risk=.02)]
    new = {c.label: c for c in ranked(battle(own, foe(), (bench,)), options(), profile, history)}
    old = {c.label: c for c in GamePlan(refined=False).apply(battle(own, foe(), (bench,)), options(), profile, {}, history, {})}
    assert old["switch"].diagnostics["plan_adjustment"] <= -75
    assert new["switch"].diagnostics["plan_adjustment"] >= -25


def kingambit():
    return pokemon("kingambit", ("swordsdance", "kowtowcleave", "suckerpunch", "ironhead"),
                   [0, 252, 0, 0, 4, 252], "adamant", "supremeoverlord", "leftovers", .8)


def test_sucker_punch_is_dropped_after_failing_into_setup_twice():
    """Replay 2697790443: Kingambit used Sucker Punch 3x into Dragon Dance."""
    profile = analyze_team(hyper_offense_team())
    nite = dragonite()
    nite._boosts.update({"atk": 2, "spe": 2})
    failed = {"action_key": "move:suckerpunch", "observations": {"own_failed": True, "last_move_foe": "dragondance"},
              "before": {"active": "kingambit", "foe_active": "dragonite"}}
    options = [move("suckerpunch", 110, ko_chance=.98, damage_low=.47), move("kowtowcleave", 20, expected_dealt=.6, faint_risk=.4)]
    result = ranked(battle(kingambit(), nite), options, profile, [failed, failed])
    assert result[0].label == "kowtowcleave"


def test_recovery_is_penalised_while_the_foe_sets_up():
    """Replays: Pelipper Roosted while Dragonite Dragon Danced and then swept."""
    profile = analyze_team(stall_team())
    pelipper = pokemon("pelipper", ("hurricane", "uturn", "roost", "surf"), [248, 0, 252, 0, 8, 0], "bold", "drizzle", "damprock", .6)
    nite = dragonite()
    nite._boosts.update({"atk": 1, "spe": 1})
    last = {"action_key": "move:hurricane", "observations": {"last_move_foe": "dragondance"},
            "before": {"active": "pelipper", "foe_active": "dragonite"}}
    options = [move("roost", 30), move("hurricane", 22, expected_dealt=.3)]
    assert ranked(battle(pelipper, nite), options, profile, [last])[0].label == "hurricane"
    calm = dragonite()
    assert ranked(battle(pelipper, calm), [move("roost", 30), move("hurricane", 22, expected_dealt=.3)], profile)[0].label == "roost"


def test_early_tera_needs_a_decisive_gain():
    """Bot replays: median Tera on turn 4 (top ladder: turn 15), often on a
    non-win-condition Great Tusk for a little extra Headlong Rush damage."""
    profile = analyze_team(hyper_offense_team())
    tusk = pokemon("greattusk", ("headlongrush", "closecombat", "icespinner", "rapidspin"),
                   [0, 252, 0, 0, 4, 252], "jolly", "protosynthesis", "boosterenergy")
    bench = [dragonite(), glimmora(), lokix(), kingambit(), foe()]
    profile["win_conditions"] = ["kingambit"]
    tera = move("headlongrush", 70, ko_chance=.4, faint_risk=.1)
    tera.terastallize = True
    plain = move("headlongrush", 62, ko_chance=.05, faint_risk=.1)
    state = battle(tusk, foe(), bench, opponent_team={f"p2: {i}": foe() for i in range(6)})
    assert not ranked(state, [tera, plain], profile)[0].terastallize
