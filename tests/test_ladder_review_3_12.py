"""Ladder review 2026-10-14 (298 games): rules derived from real losses."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from unaware.gameplan import observed_habit
from unaware.search import material_risk
from unaware.strategy import analyze_team
from test_decision_regressions import battle, pokemon
from test_gameplan import dragonite, foe, glimmora, kingambit, move, ranked, switch
from test_strategy import hyper_offense_team, stall_team

ROOT = Path(__file__).resolve().parents[1]


def frosmoth():
    return pokemon("frosmoth", ("quiverdance", "icebeam", "airslash", "substitute"),
                   [0, 0, 0, 252, 4, 252], "timid", "icescales", "heavydutyboots", .02)


def row(action, active, foe_active, foe_move="", **flags):
    return {"action_key": action, "before": {"active": active, "foe_active": foe_active},
            "observations": {"last_move_foe": foe_move, **flags}}


def test_habit_is_read_from_repeated_setup_against_sucker_punch():
    """Frosmoth answered Sucker Punch with Quiver Dance every turn."""
    history = [row("move:suckerpunch", "kingambit", "frosmoth", "quiverdance", own_failed=True)] * 3
    habit = observed_habit(history, frosmoth(), kingambit())
    assert habit["moves"] == {"quiverdance": 1.0}
    assert habit["weight"] >= .7


def test_no_habit_from_one_move_or_another_foe():
    assert observed_habit([row("move:x", "kingambit", "frosmoth", "icebeam")], frosmoth(), kingambit()) == {}
    other = [row("move:x", "kingambit", "toxapex", "toxic")] * 3
    assert observed_habit(other, frosmoth(), kingambit()) == {}


def test_habit_formed_against_another_pokemon_counts_half():
    history = [row("move:x", "zapdos", "frosmoth", "quiverdance")] * 2
    assert observed_habit(history, frosmoth(), kingambit())["weight"] == .25


def test_failed_sucker_punches_unlock_the_plain_ko():
    """Six failed Sucker Punches while Frosmoth (2%) went +1 to +5."""
    profile = analyze_team(hyper_offense_team())
    failed = row("move:suckerpunch", "kingambit", "frosmoth", "quiverdance", own_failed=True)
    options = [move("suckerpunch", -60, ko_chance=.9, damage_low=.2),
               move("kowtowcleave", -160, faint_risk=.96, damage_low=.5, accuracy=1.)]
    assert ranked(battle(kingambit(), frosmoth()), options, profile, [failed, failed])[0].label == "kowtowcleave"


def test_low_damage_pivots_count_in_the_switch_dance():
    profile = analyze_team(stall_team())
    own, bench = dragonite(), glimmora()
    pivot = {"action_key": "move:uturn", "before": {"active": "dragonite", "foe_active": "gholdengo"},
             "local_outcome": {"direct_dealt": .04}}
    swap = {"action_key": "switch:glimmora", "before": {"active": "dragonite", "foe_active": "gholdengo"}}
    calm = {c.label: c for c in ranked(battle(own, foe(), (bench,)), [move("earthquake", 40), switch(bench, 40)], profile)}
    dance = {c.label: c for c in ranked(battle(own, foe(), (bench,)), [move("earthquake", 40), switch(bench, 40)],
                                        profile, [pivot, swap, pivot])}
    assert dance["switch"].score < calm["switch"].score - 20


def test_repeated_sacrifice_into_the_same_foe_is_penalised():
    profile = analyze_team(stall_team())
    own, bench = dragonite(), glimmora()
    sack = {"action_key": "switch:glimmora", "before": {"active": "dragonite", "foe_active": "gholdengo"}}
    back = {"action_key": "switch:dragonite", "before": {"active": "glimmora", "foe_active": "gholdengo", "force_switch": True}}
    options = lambda: [move("earthquake", 40, expected_dealt=.2, faint_risk=.9), switch(bench, 40, faint_risk=.95)]
    fresh = {c.label: c for c in ranked(battle(own, foe(), (bench,)), options(), profile)}
    again = {c.label: c for c in ranked(battle(own, foe(), (bench,)), options(), profile, [sack, back])}
    assert again["switch"].score <= fresh["switch"].score - 20


def _material(own_fainted, foe_fainted):
    team = {i: SimpleNamespace(fainted=i < own_fainted) for i in range(6)}
    foes = {i: SimpleNamespace(fainted=i < foe_fainted) for i in range(6)}
    return SimpleNamespace(team=team, opponent_team=foes, teampreview_opponent_team=list(range(6)))


def test_risk_aversion_follows_the_material_score():
    assert material_risk(_material(0, 2), .35) > .5      # protect a two-Pokémon lead
    assert material_risk(_material(2, 0), .35) < .2      # behind: variance is worth more
    assert material_risk(_material(1, 1), .35) == .35


def _search(payload):
    node = ROOT / "runtime" / "node.exe"
    worker = ROOT / "unaware" / "simulator_worker.js"
    out = subprocess.run([str(node), str(worker)], input=json.dumps(payload) + "\n",
                         capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
    return json.loads(out[-1])


def _mon(species, moves, hp, item, ability, **extra):
    stats = {k: 84 for k in ("hp", "atk", "def", "spa", "spd", "spe")}
    return {"set": {"species": species, "name": "", "moves": moves, "nature": "Adamant", "evs": stats,
                    "ivs": {k: 31 for k in stats}, "level": 100, "ability": ability, "item": item, "teraType": "Normal"},
            "hp": hp, "fainted": False, "status": "", "boosts": {}, "effects": {}, "stats": {}, "priors": {},
            "revealedMoves": moves, "pp": {}, **extra}


def test_knock_off_values_removing_a_walls_leftovers():
    """Search valued Knock Off only as damage: chosen in 7% of the turns it was available."""
    own = [_mon("Ogerpon-Wellspring", ["knockoff", "powerwhip"], 1, "wellspringmask", "waterabsorb")]
    def score(item):
        foes = [_mon("Toxapex", ["toxic", "recover"], 1, item, "regenerator")]
        payload = {"op": "search", "gen": 9, "turn": 10, "own": own, "opponents": [foes],
                   "actions": [{"key": 0, "move": "knockoff"}, {"key": 1, "move": "powerwhip"}],
                   "available": [{"id": "knockoff", "pp": 32}, {"id": "powerwhip", "pp": 16}], "forceSwitch": False,
                   "trapped": False, "canTera": False, "opponentCanTera": False, "sideConditions": [{}, {}],
                   "weather": {}, "fields": {}, "depth": 1, "timeBudgetMs": 1500, "breadth": 2, "graceMs": 0,
                   "risk": .35, "feedback": {}, "hypothesisLimit": 1, "allResponses": False}
        results = {row["key"]: row["score"] for row in _search(payload)["results"]}
        return results[0] - results[1]
    assert score("blacksludge") > score("") + 5


def test_bulky_rain_abuser_is_a_win_condition():
    """Bellibolt Rain (2062 Elo, Smogon): the author's main threat is the slow,
    bulky Bellibolt, which neither sets up nor outspeeds."""
    from unaware.strategy import analyze_team as analyze
    from unaware.teams import parse_team
    profile = analyze(parse_team((ROOT / "teams" / "Bellibolt Rain 2062 by rza.txt").read_text(encoding="utf-8")))
    assert profile["archetype"] == "weather"
    assert profile["lead_species"] == "pelipper"
    assert "bellibolt" in profile["win_conditions"]
    assert "pelipper" not in profile["win_conditions"]


def test_double_ko_replacement_is_searched_not_abandoned():
    """Real state (local game, 3.12): our Basculegion and their Kingambit fell
    together. A past 1v1 snapshot used for set inference could not be rebuilt
    and the whole decision fell back to the emergency heuristic (30x on ladder)."""
    payload = json.loads((ROOT / "tests" / "fixtures" / "double_ko_force_switch.json").read_text(encoding="utf-8"))
    result = _search(payload)
    assert "error" not in result
    assert len(result["results"]) == len(payload["actions"])
