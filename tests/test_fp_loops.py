"""Battle memory: a loop without progress is noticed and left (3.19).

Case from 14/10: boosted Gliscor, Protect / Swords Dance for 13 turns next to a
Tornadus at 8% HP; Facade KOed."""
from types import SimpleNamespace

from unaware.fp_loops import alternatives, is_loop, record

MOVES = {"protect": {"category": "status"}, "swordsdance": {"category": "status"},
         "facade": {"category": "physical"}, "earthquake": {"category": "physical"}}


def battle(turn, hp=24.0, foe="tornadus"):
    return SimpleNamespace(turn=turn, opponent=SimpleNamespace(active=SimpleNamespace(name=foe, hp=hp)))


def test_third_non_damaging_move_without_progress_is_a_loop():
    memory = []
    record(memory, battle(15), "protect")
    record(memory, battle(16), "swordsdance")
    assert is_loop(memory, battle(17), "protect", MOVES)
    assert not is_loop(memory, battle(17), "facade", MOVES)  # attacking is progress


def test_no_loop_when_the_foe_loses_hp_or_changes():
    memory = []
    record(memory, battle(15, hp=60), "protect")
    record(memory, battle(16, hp=50), "protect")
    assert not is_loop(memory, battle(17, hp=40), "protect", MOVES)  # poison is working
    assert not is_loop(memory, battle(17, hp=60, foe="greattusk"), "protect", MOVES)


def test_escape_prefers_attacks_with_a_close_value():
    shares = {"protect": .87, "swordsdance": .06, "facade": .04, "earthquake": .02, "switch blissey": .003}
    values = {"protect": .53, "swordsdance": .51, "facade": .51, "earthquake": .40, "switch blissey": .39}
    assert alternatives("protect", shares, values, MOVES) == ["facade"]
