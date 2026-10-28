from types import SimpleNamespace

from unaware.fp_guards import apply, destiny_bond_trade, locked_into_resisted

MOVES = {"earthquake": {"category": "physical", "type": "ground"}, "dragondance": {"category": "status", "type": "dragon"},
         "darkpulse": {"category": "special", "type": "dark"}, "icebeam": {"category": "special", "type": "ice"}}
CHART = {("dark", ("psychic", "dark")): .5, ("dark", ("fighting",)): .5, ("dark", ("psychic",)): 2.}


def modifier(move_type, types):
    return CHART.get((move_type, tuple(types)), 1.)


def mon(hp, max_hp=100, types=("dragon",), volatile=(), item="", moves=()):
    return SimpleNamespace(hp=hp, max_hp=max_hp, types=list(types), terastallized=False, tera_type=None,
                           volatile_statuses=list(volatile), item=item, moves=list(moves))


def move(name, disabled=False):
    return SimpleNamespace(name=name, disabled=disabled, current_pp=8)


def battle(user, foe):
    return SimpleNamespace(user=SimpleNamespace(active=user), opponent=SimpleNamespace(active=foe))


def test_no_attack_into_destiny_bond_with_the_healthier_pokemon():
    b = battle(mon(385, 385), mon(1, 260, types=("ghost", "poison"), volatile=["destinybond"]))
    assert destiny_bond_trade("earthquake", b, MOVES)
    assert not destiny_bond_trade("dragondance", b, MOVES)
    policy = [("earthquake", .4), ("dragondance", .35), ("switch kyurem", .1)]
    assert apply(policy, b, move_json=MOVES, type_modifier=modifier) == policy[1:]


def test_destiny_bond_trade_is_fine_when_ours_is_nearly_gone():
    b = battle(mon(10, 385), mon(200, 260, volatile=["destinybond"]))
    assert not destiny_bond_trade("earthquake", b, MOVES)


def test_choice_locked_into_a_resist_switches():
    darkrai = mon(281, 281, types=("dark",), item="choicescarf",
                  moves=[move("darkpulse"), move("icebeam", True), move("willowisp", True), move("trick", True)])
    hoopa = mon(100, 100, types=("psychic", "dark"))
    b = battle(darkrai, hoopa)
    assert locked_into_resisted("darkpulse", b, MOVES, modifier)
    policy = [("darkpulse", .9), ("switch hatterene", .06), ("switch kyurem", .04)]
    assert apply(policy, b, move_json=MOVES, type_modifier=modifier) == policy[1:]


def test_lock_stays_when_it_hits_well_or_the_foe_is_low():
    darkrai = mon(281, 281, types=("dark",), item="choicescarf", moves=[move("darkpulse"), move("icebeam", True)])
    assert not locked_into_resisted("darkpulse", battle(darkrai, mon(100, 100, types=("psychic",))), MOVES, modifier)
    assert not locked_into_resisted("darkpulse", battle(darkrai, mon(30, 100, types=("psychic", "dark"))), MOVES, modifier)
    unlocked = mon(281, 281, types=("dark",), item="choicescarf", moves=[move("darkpulse"), move("icebeam")])
    assert not locked_into_resisted("darkpulse", battle(unlocked, mon(100, 100, types=("psychic", "dark"))), MOVES, modifier)
