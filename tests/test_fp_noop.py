"""Moves certain to do nothing leave Foul Play's final choice (3.15).

Cases from the 211-turn loss to wpom's stall on 14/10."""
from collections import defaultdict
from types import SimpleNamespace

import sys

from unaware.foulplay_brain import VENDOR
from unaware.fp_noop import filter_policy, is_noop

if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


def mon(name, types, status=None, tera=None, ability=None, item="unknownitem", hp=100):
    return SimpleNamespace(name=name, types=types, status=status, terastallized=bool(tera),
                           tera_type=tera, ability=ability, item=item, hp=hp, volatile_statuses=[])


def battle(user, foe, foe_side=None):
    side = defaultdict(int, foe_side or {})
    return SimpleNamespace(battle_tag="t", user=SimpleNamespace(active=user),
                           opponent=SimpleNamespace(active=foe, side_conditions=side))


BLISSEY = mon("blissey", ["normal"], ability="naturalcure")
PECHARUNT = mon("pecharunt", ["poison", "ghost"], ability="poisonpuppeteer")
SKARMORY = mon("skarmory", ["steel", "flying"], ability="sturdy")


def test_shadow_ball_into_tera_normal_alomomola():
    """Turns 33-60: 11 Shadow Balls into an Alomomola that had Tera'd Normal."""
    alomomola = mon("alomomola", ["water"], tera="normal")
    assert is_noop("shadowball", battle(BLISSEY, alomomola))
    assert not is_noop("shadowball", battle(BLISSEY, mon("alomomola", ["water"])))
    assert not is_noop("seismictoss", battle(BLISSEY, alomomola))


def test_shadow_ball_into_blissey_and_toxic_into_poisoned():
    """Turns 72-107: Pecharunt alternated Shadow Ball into Blissey and Toxic into a poisoned Gliscor."""
    assert is_noop("shadowball", battle(PECHARUNT, BLISSEY))
    gliscor = mon("gliscor", ["ground", "flying"], status="tox")
    assert is_noop("toxic", battle(PECHARUNT, gliscor))
    assert not is_noop("toxic", battle(PECHARUNT, mon("gliscor", ["ground", "flying"])))
    assert is_noop("toxic", battle(PECHARUNT, mon("goodrahisui", ["steel", "dragon"])))


def test_hazard_already_up_and_good_as_gold():
    assert is_noop("stealthrock", battle(SKARMORY, BLISSEY, {"stealthrock": 1}))
    assert not is_noop("stealthrock", battle(SKARMORY, BLISSEY))
    assert not is_noop("spikes", battle(SKARMORY, BLISSEY, {"spikes": 2}))
    # Gholdengo has only Good as Gold: status moves fail even before it is revealed.
    assert is_noop("toxic", battle(PECHARUNT, mon("gholdengo", ["steel", "ghost"])))


def test_exceptions_that_do_hit():
    scrappy = mon("kangaskhan", ["normal"], ability="scrappy")
    assert not is_noop("bodyslam", battle(scrappy, mon("gengar", ["ghost", "poison"])))
    assert not is_noop("switch blissey", battle(PECHARUNT, BLISSEY))
    assert not is_noop("recover", battle(PECHARUNT, BLISSEY))
    assert not is_noop("partingshot", battle(PECHARUNT, BLISSEY))


def test_filter_keeps_choice_when_search_has_nothing_close():
    b = battle(PECHARUNT, BLISSEY)
    policy = [("shadowball", .54), ("recover", .25), ("toxic", .21)]
    assert filter_policy(policy, b) == [("recover", .25), ("toxic", .21)]
    lonely = [("shadowball", .90), ("recover", .10)]
    assert filter_policy(lonely, b) == lonely
    assert filter_policy([("shadowball-tera", .6), ("partingshot", .4)], b) == [("partingshot", .4)]
