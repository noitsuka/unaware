from types import SimpleNamespace

import pytest
from poke_env.battle import Field, Move, Pokemon, PokemonType, SideCondition, Status, Weather

from unaware.engine import DecisionEngine, FEATURE_NAMES, _opponents


class Meta:
    def __init__(self, data=None):
        self.data = data or {}

    def species(self, name):
        return self.data.get(name, {})


def pokemon(species, moves, hp=1, ability=None, item=None, stats=None):
    mon = Pokemon(9, species=species)
    mon.moves.update({name: Move(name, gen=9) for name in moves})
    mon._stats = stats or {"hp": 350, "atk": 280, "def": 230, "spa": 280, "spd": 230, "spe": 280}
    mon._max_hp = mon.stats["hp"]
    mon._current_hp = round(mon.max_hp * hp)
    mon._ability = ability
    mon._item = item
    mon._active_turns = 1
    return mon


def battle(own, foe, bench=(), opposing=(), **kwargs):
    fields = dict(gen=9, active_pokemon=own, opponent_active_pokemon=foe,
                  available_moves=list(own.moves.values()) if own else [],
                  available_switches=list(bench), force_switch=False, trapped=False,
                  can_tera=False, team={str(i): p for i, p in enumerate([own, *bench]) if p},
                  opponent_team={str(i): p for i, p in enumerate([foe, *opposing]) if p},
                  side_conditions={}, opponent_side_conditions={}, fields={}, weather={}, turn=3,
                  battle_tag="test-battle")
    fields.update(kwargs)
    return SimpleNamespace(**fields)


def engine_for(foe, **kwargs):
    return DecisionEngine(Meta({foe.species: {"Moves": {m.id: 1 for m in foe.moves.values()}}}), **kwargs)


def test_does_not_thunderbolt_a_ground_type():
    own = pokemon("starmie", ["thunderbolt", "icebeam"])
    foe = pokemon("landorustherian", ["earthquake"])
    b = battle(own, foe)
    policy = engine_for(foe)
    assert policy.choose(b).action.id == "icebeam"
    assert policy._damage(own, foe, own.moves["thunderbolt"], b).high == 0


def test_levitate_and_mold_breaker_are_respected():
    own = pokemon("excadrill", ["earthquake", "ironhead"], ability="sandrush")
    foe = pokemon("rotomwash", ["hydropump"], ability="levitate")
    b = battle(own, foe)
    policy = engine_for(foe)
    assert policy._damage(own, foe, own.moves["earthquake"], b).high == 0
    own._ability = "moldbreaker"
    policy._reset()
    assert policy._damage(own, foe, own.moves["earthquake"], b).high > 0


def test_forced_switch_returns_only_legal_replacements():
    own = pokemon("pikachu", ["thunderbolt"], hp=0)
    own.status = Status.FNT
    flying = pokemon("corviknight", ["bravebird"], ability="pressure")
    weak = pokemon("heatran", ["flamethrower"], ability="flashfire")
    foe = pokemon("garchomp", ["earthquake"])
    ranked = engine_for(foe).rank_actions(battle(own, foe, [flying, weak], force_switch=True))
    assert all(isinstance(choice.action, Pokemon) for choice in ranked)
    assert ranked[0].action is flying


def test_trapped_never_offers_switch_and_only_server_moves_are_used():
    own = pokemon("gengar", ["shadowball", "thunderbolt"])
    foe = pokemon("blissey", ["seismictoss"])
    replacement = pokemon("greattusk", ["closecombat"])
    b = battle(own, foe, [replacement], trapped=True, available_moves=[own.moves["thunderbolt"]])
    ranked = engine_for(foe).rank_actions(b)
    assert len(ranked) == 1 and ranked[0].action.id == "thunderbolt"


def test_struggle_is_legal_with_empty_known_moves():
    own = pokemon("pikachu", [])
    foe = pokemon("blissey", ["seismictoss"])
    b = battle(own, foe, available_moves=[Move("struggle", gen=9)])
    assert engine_for(foe).choose(b).action.id == "struggle"


def test_priority_secures_ko_before_faster_foe():
    own = pokemon("kingambit", ["suckerpunch", "ironhead"], hp=0.15)
    own._stats["spe"] = 100
    foe = pokemon("dragapult", ["flamethrower"], hp=0.12)
    foe._stats["spe"] = 400
    assert engine_for(foe).choose(battle(own, foe)).action.id == "suckerpunch"


def test_thunder_wave_ground_and_toxic_steel_immunities():
    own = pokemon("blissey", ["toxic", "thunderwave", "seismictoss"])
    foe = pokemon("excadrill", ["swordsdance"])
    b = battle(own, foe)
    policy = engine_for(foe)
    assert policy._status_value(own.moves["toxic"], own, foe, b) == 0
    assert policy._status_value(own.moves["thunderwave"], own, foe, b) == 0
    assert policy.choose(b).action.id == "seismictoss"


def test_will_not_recover_at_full_health():
    own = pokemon("starmie", ["recover", "surf"])
    foe = pokemon("charizard", ["flamethrower"])
    assert engine_for(foe).choose(battle(own, foe)).action.id == "surf"


def test_healing_is_valuable_when_safe_and_injured():
    own = pokemon("toxapex", ["recover", "surf"], hp=0.4)
    own._stats["spa"] = 130
    foe = pokemon("blissey", ["tackle"])
    foe._stats["atk"] = 20
    foe._stats["spd"] = 350
    assert engine_for(foe).choose(battle(own, foe)).action.id == "recover"


def test_tera_is_only_offered_when_server_allows_and_can_improve_damage():
    own = pokemon("dragonite", ["extremespeed"])
    own._terastallized_type = PokemonType.NORMAL
    foe = pokemon("blissey", ["seismictoss"], hp=0.35)
    policy = engine_for(foe)
    b = battle(own, foe, can_tera=True)
    ranked = policy.rank_actions(b)
    assert len(ranked) == 2 and {r.terastallize for r in ranked} == {True, False}
    assert ranked[0].terastallize
    assert not own.is_terastallized
    b.can_tera = False
    assert not any(r.terastallize for r in policy.rank_actions(b))


def test_switch_hazards_avoid_sacrificing_low_health_pokemon():
    own = pokemon("pikachu", [], hp=0)
    own.status = Status.FNT
    fragile = pokemon("charizard", ["flamethrower"], hp=0.4)
    safe = pokemon("garchomp", ["earthquake"], hp=0.6)
    foe = pokemon("blissey", ["seismictoss"])
    b = battle(own, foe, [fragile, safe], force_switch=True,
               side_conditions={SideCondition.STEALTH_ROCK: 1})
    assert engine_for(foe).choose(b).action is safe
    fragile.item = "heavydutyboots"
    policy = engine_for(foe)
    assert policy._hazards(fragile, b) == 0


def test_known_stats_not_overwritten_and_hidden_meta_estimates_are_private():
    own = pokemon("garchomp", ["earthquake"])
    foe = Pokemon(9, species="heatran")
    meta = Meta({"heatran": {"Spreads": {"Calm:252/0/0/4/252/0": 10},
                              "Abilities": {"Flash Fire": 10}, "Items": {"Leftovers": 10},
                              "Moves": {"Magma Storm": 10, "Earth Power": 9}}})
    policy = DecisionEngine(meta)
    before = dict(foe.stats)
    policy.rank_actions(battle(own, foe))
    inferred = policy._snapshot(foe)
    assert foe.stats == before and foe.ability is None
    assert inferred.stats["hp"] > 300 and inferred.ability == "flashfire"
    assert inferred.item == "leftovers"
    assert policy._snapshot(own).stats == own.stats
    assert {m.id for m, _ in policy._moves(foe, True)} == {"magmastorm", "earthpower"}


def test_weather_burn_boosts_and_accuracy_change_damage():
    own = pokemon("charizard", ["flamethrower", "flareblitz", "fireblast"])
    foe = pokemon("mew", ["psychic"])
    policy = engine_for(foe)
    b = battle(own, foe)
    normal = policy._damage(own, foe, own.moves["flamethrower"], b).expected
    b.weather = {Weather.SUNNYDAY: 1}
    policy._reset()
    assert policy._damage(own, foe, own.moves["flamethrower"], b).expected > normal * 1.4
    full = policy._damage(own, foe, own.moves["flareblitz"], b).expected
    own.status = Status.BRN
    policy._reset()
    assert policy._damage(own, foe, own.moves["flareblitz"], b).expected < full * 0.6
    own.boosts["spa"] = 2
    policy._reset()
    assert policy._damage(own, foe, own.moves["flamethrower"], b).expected > normal * 2.5
    assert policy._damage(own, foe, own.moves["fireblast"], b).accuracy == pytest.approx(0.85)


def test_trick_room_changes_move_order():
    own = pokemon("slowbro", ["surf"])
    own._stats["spe"] = 80
    foe = pokemon("jolteon", ["thunderbolt"])
    foe._stats["spe"] = 380
    b = battle(own, foe)
    policy = engine_for(foe)
    assert policy._first(own, own.moves["surf"], foe, foe.moves["thunderbolt"], b) == 0
    b.fields = {Field.TRICK_ROOM: 1}
    assert policy._first(own, own.moves["surf"], foe, foe.moves["thunderbolt"], b) == 1


def test_preview_order_contains_every_roster_slot_once():
    own = pokemon("greattusk", ["earthquake", "stealthrock"])
    bench = [pokemon("kingambit", ["suckerpunch"]), pokemon("corviknight", ["uturn"])]
    foe = pokemon("heatran", ["flamethrower"])
    order = engine_for(foe).preview_order(battle(own, foe, bench))
    assert sorted(order) == [1, 2, 3]


def test_unidentified_opponent_and_empty_request_are_handled():
    own = pokemon("pikachu", ["thunderbolt"])
    policy = DecisionEngine()
    assert policy.choose(battle(own, None)).action.id == "thunderbolt"
    with pytest.raises(ValueError, match="legal action"):
        policy.choose(battle(own, None, available_moves=[]))


def test_always_hit_and_no_guard_still_allow_full_paralysis():
    own = pokemon("machamp", ["aurasphere", "dynamicpunch"], ability="noguard")
    foe = pokemon("blissey", ["tackle"])
    own.status = Status.PAR
    policy = engine_for(foe)
    b = battle(own, foe)
    for move in own.moves.values():
        assert policy._damage(own, foe, move, b).accuracy == pytest.approx(0.75)


def test_preview_unseen_bench_is_retained_and_revealed_faint_replaces_preview():
    own = pokemon("garchomp", ["stealthrock", "earthquake"])
    first = pokemon("heatran", ["flamethrower"])
    dead = pokemon("rotomwash", ["hydropump"], hp=0)
    dead.status = Status.FNT
    unseen = Pokemon(9, species="corviknight")
    b = battle(own, first, opposing=[dead], teampreview_opponent_team=[
        Pokemon(9, species="heatran"), Pokemon(9, species="rotomwash"), unseen])
    roster = _opponents(b)
    assert roster == [first, dead, unseen]
    assert sum(not p.fainted for p in roster) == 2
    candidate = next(c for c in engine_for(first).rank_actions(b) if c.action.id == "stealthrock")
    assert candidate.features["hazards"] == 1


def test_hazards_break_sash_for_switch_damage_estimate():
    own = pokemon("pikachu", ["thunderbolt"])
    switch = pokemon("alakazam", ["psychic"], item="focussash", ability="synchronize")
    switch._stats["def"] = 80
    foe = pokemon("garchomp", ["earthquake"])
    foe._stats["atk"] = 500
    b = battle(own, foe, [switch], side_conditions={SideCondition.STEALTH_ROCK: 1})
    policy = engine_for(foe)
    damage = policy._damage(foe, switch, foe.moves["earthquake"], b, switched_defender=True)
    assert damage.ko_chance(0.875) == 1
    assert switch.current_hp_fraction == 1  # Estimate never changes server state.


def test_record_choice_penalizes_repeated_voluntary_switches():
    own = pokemon("pikachu", ["thunderbolt"])
    switch = pokemon("corviknight", ["bravebird"])
    foe = pokemon("garchomp", ["earthquake"])
    b = battle(own, foe, [switch])
    policy = engine_for(foe)
    first = next(c for c in policy.rank_actions(b) if c.action is switch)
    policy.record_choice(b, first)
    b.turn += 1
    second = next(c for c in policy.rank_actions(b) if c.action is switch)
    assert second.score == pytest.approx(first.score - 10)


def test_feature_contract_is_bounded_and_estimates_remain_base_after_adjustment():
    own = pokemon("dragonite", ["extremespeed", "roost", "dragondance"], hp=0.5)
    own._terastallized_type = PokemonType.NORMAL
    switch = pokemon("corviknight", ["uturn"])
    foe = pokemon("blissey", ["seismictoss"], hp=0.35)
    b = battle(own, foe, [switch], can_tera=True)
    policy = engine_for(foe)
    ranked = policy.rank_actions(b)
    original = [(c.label, c.score) for c in ranked]
    for candidate in ranked:
        assert set(candidate.features) == set(FEATURE_NAMES)
        assert all(0 <= value <= 1 for value in candidate.features.values())
        assert candidate.diagnostics["has_damage_estimate"] == 1
        assert 0 <= candidate.diagnostics["expected_dealt"] <= 1
        assert 0 <= candidate.diagnostics["expected_received"] <= 1
        old = candidate.score
        candidate.score += 10
        assert candidate.diagnostics["base_score"] == old
    assert [(c.label, c.score) for c in policy.rank_actions(b)] == original


def test_forced_switch_diagnostics_forecast_only_entry_damage():
    own = pokemon("pikachu", [], hp=0)
    own.status = Status.FNT
    switch = pokemon("charizard", ["flamethrower"])
    foe = pokemon("garchomp", ["stoneedge"])
    b = battle(own, foe, [switch], force_switch=True,
               side_conditions={SideCondition.STEALTH_ROCK: 1})
    candidate = engine_for(foe).choose(b)
    assert candidate.diagnostics["expected_received"] == pytest.approx(0.5)
    assert candidate.diagnostics["expected_dealt"] == 0
    assert candidate.diagnostics["forced_switch"] == 1


def test_sucker_punch_cannot_hit_a_healing_action():
    own = pokemon("blissey", ["softboiled"], hp=0.3)
    foe = pokemon("kingambit", ["suckerpunch"])
    chosen = engine_for(foe).choose(battle(own, foe))
    assert chosen.diagnostics["expected_received"] == 0


def test_protect_prevents_normal_attack_estimate():
    own = pokemon("garchomp", ["earthquake"])
    foe = pokemon("blissey", ["protect"])
    chosen = engine_for(foe).choose(battle(own, foe))
    assert chosen.diagnostics["expected_dealt"] == 0


def test_unknown_foe_has_explicit_unavailable_estimate():
    own = pokemon("pikachu", ["thunderbolt"])
    candidate = DecisionEngine().choose(battle(own, None))
    assert candidate.diagnostics["has_damage_estimate"] == 0
    assert candidate.diagnostics["base_score"] == candidate.score
    assert set(candidate.features) == set(FEATURE_NAMES)
