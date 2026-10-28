"""Entry initiative and health accounting stay consistent with the next turn."""
import pytest
from poke_env.battle import Effect, Field, SideCondition

from unaware.engine import Candidate, DecisionEngine
from unaware.policy import BattlePolicy
from test_decision_regressions import battle
from test_policy_v4 import mon


def entry_order(*, item="", ability="", own_conditions=None, foe_conditions=None,
                species="gengar", fields=None, old_boost=0):
    actor = mon(species, ("shadowball",), item=item, ability=ability)
    foe = mon(species, ("shadowball",))
    actor.boosts["spe"] = old_boost
    state = battle(mon("mew", ("psychic",), hp=0), foe, [actor],
                   side_conditions=own_conditions or {},
                   opponent_side_conditions=foe_conditions or {}, fields=fields or {})
    value = BattlePolicy.entry_first(state, actor, actor.moves["shadowball"], foe,
                                    foe.moves["shadowball"], DecisionEngine(), 1.)
    assert actor.boosts["spe"] == old_boost
    return value


@pytest.mark.parametrize("item,ability,expected", [
    ("", "", 0.),
    ("heavydutyboots", "", .5),
    ("clearamulet", "", .5),
    ("airballoon", "", .5),
    ("", "levitate", .5),
    ("", "clearbody", .5),
    ("", "whitesmoke", .5),
    ("", "fullmetalbody", .5),
    ("", "mirrorarmor", .5),
    ("", "contrary", 1.),
    ("", "simple", 0.),
])
def test_entry_web_respects_items_and_abilities(item, ability, expected):
    assert entry_order(item=item, ability=ability,
                       own_conditions={SideCondition.STICKY_WEB: 1}) == expected


def test_flying_entry_ignores_web_until_gravity_grounds_it():
    assert entry_order(species="tornadus", own_conditions={SideCondition.STICKY_WEB: 1}) == .5
    assert entry_order(species="tornadus", own_conditions={SideCondition.STICKY_WEB: 1},
                       fields={Field.GRAVITY: 1}) == 0.


def test_entry_clears_previous_speed_boost_and_keeps_each_sides_tailwind():
    assert entry_order(old_boost=6) == .5
    assert entry_order(old_boost=-6) == .5
    assert entry_order(own_conditions={SideCondition.STICKY_WEB: 1, SideCondition.TAILWIND: 1}) == 1.
    assert entry_order(foe_conditions={SideCondition.TAILWIND: 1}) == 0.
    assert entry_order(own_conditions={SideCondition.TAILWIND: 1},
                       foe_conditions={SideCondition.TAILWIND: 1}) == .5


def test_entry_web_respects_trick_room_and_does_not_invert_ties():
    assert entry_order(own_conditions={SideCondition.STICKY_WEB: 1},
                       fields={Field.TRICK_ROOM: 1}) == 1.
    assert entry_order(item="heavydutyboots", own_conditions={SideCondition.STICKY_WEB: 1},
                       fields={Field.TRICK_ROOM: 1}) == .5


def test_forced_entry_matchup_matches_actual_webbed_next_turn():
    actor = mon("gengar", ("shadowball",), hp=.1)
    foe = mon("mew", ("psychic", "icebeam", "flamethrower", "shadowball"), hp=.1)
    state = battle(mon("mew", ("psychic",), hp=0), foe, [actor],
                   side_conditions={SideCondition.STICKY_WEB: 1}, force_switch=True)
    before = (dict(actor.boosts), dict(actor.effects), actor.current_hp)
    predicted = BattlePolicy.next_matchup(state, actor, foe, DecisionEngine(), .1, switched=True)
    assert (dict(actor.boosts), dict(actor.effects), actor.current_hp) == before
    actor.boosts["spe"] = -1
    observed = BattlePolicy.next_matchup(state, actor, foe, DecisionEngine(), .1)
    assert predicted == pytest.approx(observed)
    assert predicted == pytest.approx((0., 1.))


def test_entry_drops_temporary_airborne_effect_and_gale_wings_after_hazards():
    actor, foe = mon("talonflame", ("bravebird",), ability="galewings"), mon("gengar", ("shadowball",))
    # Intact Gale Wings grants priority, then hazards remove that priority.
    state = battle(mon("mew", ("psychic",), hp=0), foe, [actor])
    engine = DecisionEngine()
    assert BattlePolicy.entry_first(state, actor, actor.moves["bravebird"], foe, foe.moves["shadowball"], engine, 1.) == 1.
    # Give the foe a Scarf so entry damage removing Gale Wings changes the order.
    foe._item = "choicescarf"
    assert BattlePolicy.entry_first(state, actor, actor.moves["bravebird"], foe, foe.moves["shadowball"], DecisionEngine(), .5) == 0.
    actor = mon("gengar", ("shadowball",))
    actor.effects[Effect.MAGNET_RISE] = 1
    foe = mon("gengar", ("shadowball",))
    state = battle(mon("mew", ("psychic",), hp=0), foe, [actor], side_conditions={SideCondition.STICKY_WEB: 1})
    assert BattlePolicy.entry_first(state, actor, actor.moves["shadowball"], foe, foe.moves["shadowball"], DecisionEngine(), 1.) == 0.
    assert Effect.MAGNET_RISE in actor.effects


def effect_value(actor, foe, move, damage):
    candidate = Candidate(actor.moves[move], diagnostics={"expected_dealt": damage})
    vector, _ = BattlePolicy().features(battle(actor, foe), candidate, DecisionEngine(), {}, {})
    return vector


def test_drain_credits_actual_healed_hp_for_targets_with_different_max_hp():
    actor = mon("comfey", ("drainingkiss",), hp=.25, ability="triage")
    large, small = mon("blissey", ("seismictoss",)), mon("gengar", ("shadowball",))
    for foe in (large, small):
        # An observed 40 HP of direct damage restores 30 HP via Draining Kiss,
        # regardless of what fraction of this particular target's HP it was.
        vector = effect_value(actor, foe, "drainingkiss", 40 / foe.max_hp)
        assert vector["heal"] == pytest.approx(30 / actor.max_hp)
    actor._current_hp = actor.max_hp - 10
    assert effect_value(actor, large, "drainingkiss", 40 / large.max_hp)["heal"] == pytest.approx(10 / actor.max_hp)


def test_recoil_credits_actual_self_damage_for_targets_with_different_max_hp():
    actor = mon("raikou", ("wildcharge",))
    for foe in (mon("blissey", ("seismictoss",)), mon("gengar", ("shadowball",))):
        # Wild Charge's recoil from 100 HP of damage is 25 of the user's HP.
        vector = effect_value(actor, foe, "wildcharge", 100 / foe.max_hp)
        assert vector["resource"] == pytest.approx(25 / actor.max_hp)


@pytest.mark.parametrize("ability", ["rockhead", "magicguard"])
def test_recoil_preventing_abilities_do_not_receive_fake_cost(ability):
    actor = mon("mew", ("wildcharge",), ability=ability)
    foe = mon("blissey", ("seismictoss",))
    assert effect_value(actor, foe, "wildcharge", 100 / foe.max_hp)["resource"] == 0.


@pytest.mark.parametrize("ability", ["", "rockhead", "magicguard"])
def test_struggle_is_based_on_own_hp_and_bypasses_recoil_protection(ability):
    actor, foe = mon("mew", ("struggle",), ability=ability), mon("blissey", ("seismictoss",))
    for damage in (.01, .5):
        assert effect_value(actor, foe, "struggle", damage)["resource"] == pytest.approx(.25)


@pytest.mark.parametrize("ability,cost", [("", True), ("rockhead", True), ("magicguard", False)])
def test_liquid_ooze_reverses_drain_even_at_full_hp_without_fake_healing(ability, cost):
    foe = mon("tentacruel", ("surf",), ability="liquidooze")
    for hp in (.25, 1.):
        actor = mon("mew", ("drainingkiss",), hp=hp, ability=ability)
        vector = effect_value(actor, foe, "drainingkiss", 40 / foe.max_hp)
        assert vector["heal"] == 0.
        assert vector["resource"] == pytest.approx(30 / actor.max_hp if cost else 0.)


@pytest.mark.parametrize("ability", ["", "magicguard"])
def test_strength_sap_does_not_heal_against_liquid_ooze(ability):
    actor = mon("mew", ("strengthsap",), hp=.5, ability=ability)
    foe = mon("tentacruel", ("surf",), ability="liquidooze")
    vector = effect_value(actor, foe, "strengthsap", 0.)
    assert vector["heal"] == 0.
    assert (vector["resource"] > 0) is (ability != "magicguard")
