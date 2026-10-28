"""Sure KO on a weakened foe beats Foul Play's stalling (3.16).

Case from 14/10: +2 Gliscor (Tera Normal, Facade) vs Tornadus at 8% HP chose
Protect for turns 15-26 and lost the game."""
from types import SimpleNamespace

from poke_env.battle import Move

from unaware.engine import Candidate
from unaware.sure_ko import override


def move(move_id, ko=0.0, accuracy=1.0, faint_risk=0.0, tera=False):
    return Candidate(Move(move_id, gen=9), terastallize=tera, label=move_id + (" + Tera" if tera else ""),
                     diagnostics={"ko_chance": ko, "accuracy": accuracy, "faint_risk": faint_risk})


def battle(foe_hp):
    foe = SimpleNamespace(species="tornadus", fainted=False, current_hp_fraction=foe_hp)
    return SimpleNamespace(opponent_active_pokemon=foe, turn=15)


def test_protect_becomes_the_sure_ko_facade():
    protect, facade = move("protect"), move("facade", ko=1.0)
    candidates = [protect, move("swordsdance"), move("earthquake"), facade]
    assert override(battle(.08), protect, candidates) is facade


def test_nothing_changes_when_the_ko_is_not_sure_or_the_foe_is_healthy():
    protect = move("protect")
    assert override(battle(.08), protect, [protect, move("facade", ko=.9)]) is None
    assert override(battle(.8), protect, [protect, move("facade", ko=1.0)]) is None  # Sash, Sturdy, Multiscale
    assert override(battle(.08), protect, [protect, move("focusblast", ko=1.0, accuracy=.7)]) is None
    assert override(battle(.08), protect, [protect, move("facade", ko=1.0, tera=True)]) is None  # no Tera for it


def test_attacks_and_switches_foul_play_chose_are_kept():
    earthquake, facade = move("earthquake", ko=.5), move("facade", ko=1.0)
    assert override(battle(.08), earthquake, [earthquake, facade]) is None
    switch = Candidate(SimpleNamespace(species="blissey"), label="Switch: blissey")
    assert override(battle(.08), switch, [switch, facade]) is None


def test_fixed_damage_counts_as_an_attack():
    softboiled, toss = move("softboiled"), move("seismictoss", ko=1.0)
    assert override(battle(.2), softboiled, [softboiled, toss]) is toss
