"""Scouting prior on Foul Play's set sampling (3.14).

Offline on the 51 ladder games of 14/10 Foul
Play's team-preview samples contained 59.7% -> 65.4% of the moves the opponent
then used, and 36.5% -> 53.3% of their revealed items."""

from unaware import foulplay_brain
from unaware.fp_scouting import apply_prior, scouted_sets

foulplay_brain.configure("scouttest", "gen9ou", 100, 1)

from fp.data.sets import PokemonMoveset, PokemonSet, PredictedPokemonSet  # noqa: E402


def game(gid, species, revealed):
    return {"id": gid, "team": {"species": species, "revealed": revealed}}


TEAM = ["gholdengo", "greattusk", "kingambit", "dragapult", "slowkinggalar", "rillaboom"]
SHOWN = {"species": "gholdengo", "moves": ["nastyplot", "shadowball"], "items": ["airballoon"], "tera_types": [],
         "abilities": [], "roles": []}


def test_only_games_with_the_same_team_count():
    other = ["gholdengo", "zamazenta", "ironvaliant", "landorustherian", "kyurem", "ogerpon"]
    prior = scouted_sets([game("a", other, [SHOWN])], TEAM)
    assert prior == {}  # one species in common: a different team
    prior = scouted_sets([game("a", TEAM[:4] + ["garchomp", "toxapex"], [SHOWN])], TEAM)
    assert prior["gholdengo"]["moves"]["nastyplot"] == 1 and prior["gholdengo"]["items"]["airballoon"] == 1
    assert scouted_sets([game("a", TEAM, [SHOWN])], TEAM, exclude_ids={"a"}) == {}


def predicted(item, moves):
    return PredictedPokemonSet(pkmn_set=PokemonSet(ability="goodasgold", item=item, nature="timid",
                                                   evs=(0, 0, 0, 252, 4, 252), count=1, tera_type="fairy"),
                               pkmn_moveset=PokemonMoveset(moves=tuple(moves)))


class Datasets:
    def __init__(self):
        self.pkmn_sets = {"gholdengo": [predicted("choicescarf", ["makeitrain", "shadowball", "trick", "recover"])] * 3
                          + [predicted("airballoon", ["makeitrain", "shadowball", "nastyplot", "recover"])]}
        self.raw_pkmn_moves = {"gholdengo": [PokemonMoveset(moves=("makeitrain", "shadowball"), count=10)]}


def test_agreeing_sets_become_most_draws_and_nothing_is_removed():
    data = Datasets()
    prior = scouted_sets([game("a", TEAM, [SHOWN])], TEAM)
    assert apply_prior(data, prior) == {"gholdengo": 1}
    sets = data.pkmn_sets["gholdengo"]
    balloon = sum(s.pkmn_set.item == "airballoon" for s in sets)
    assert balloon / len(sets) >= .75
    assert any(s.pkmn_set.item == "choicescarf" for s in sets)  # a changed set still has public data
    shown = data.raw_pkmn_moves["gholdengo"][-1]
    assert set(shown.moves) == {"nastyplot", "shadowball"} and shown.count == 30


def test_bridge_applies_the_prior_once_per_scouting():
    """Foul Play's mode survives deepcopy as the same object: the prior must
    not compound turn after turn, and new scouting replaces the old prior."""
    import fp.run_battle as fp_run
    import fp.search.standard_battles as sb
    from fp.constants import BattleType
    holder = {"games": [game("a", TEAM, [SHOWN])]}
    foulplay_brain._LOCAL.scouting, foulplay_brain._LOCAL.search = holder, None
    mode = fp_run.battle_mode(BattleType.STANDARD_BATTLE)
    foulplay_brain._LOCAL.scouting = None
    mode.team_datasets = Datasets()

    class Opp:
        def __init__(self, name):
            self.name = name

    class Battle:
        battle_tag = "battle-test"
        opponent = type("S", (), {"active": Opp("gholdengo"), "reserve": [Opp(n) for n in TEAM[1:]]})()

    real = sb.prepare_battles
    sb.prepare_battles = lambda battle, n: []
    try:
        mode.prepare_battles(Battle(), 1)
        first = len(mode.team_datasets.pkmn_sets["gholdengo"])
        moves = len(mode.team_datasets.raw_pkmn_moves["gholdengo"])
        for _ in range(5):
            mode.prepare_battles(Battle(), 1)
        assert len(mode.team_datasets.pkmn_sets["gholdengo"]) == first > 4
        assert len(mode.team_datasets.raw_pkmn_moves["gholdengo"]) == moves == 2
        holder["games"] = holder["games"] + [game("b", TEAM, [{**SHOWN, "items": ["choicescarf"], "moves": ["trick"]}])]
        mode.prepare_battles(Battle(), 1)
        assert len(mode.team_datasets.raw_pkmn_moves["gholdengo"]) == 2  # rebuilt, not stacked
    finally:
        sb.prepare_battles = real
