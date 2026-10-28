"""Tactical regressions prompted by battle-gen9ru-2690941980.

These use legal level-100 spreads rather than making every species' stats
identical. Opponent spreads are explicit fixtures, not claimed hidden knowledge.
Tests also include nearby endgames with objectively decisive priority moves.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

from poke_env.battle import Battle, Move, Pokemon, PokemonType, SideCondition, Status
from poke_env.data import GenData
from poke_env.stats import compute_raw_stats

from unaware.engine import Candidate, DecisionEngine
from unaware.telemetry import BattleTrace, snapshot


STATS = ("hp", "atk", "def", "spa", "spd", "spe")


def pokemon(species, moves, evs, nature, ability, item="", hp=1.0, tera=None):
    mon = Pokemon(9, species=species)
    mon.moves.update({move: Move(move, gen=9) for move in moves})
    mon._stats = dict(zip(STATS, compute_raw_stats(
        species, evs, [31] * 6, 100, nature, GenData.from_gen(9))))
    mon._evs, mon._ivs, mon._nature = evs, [31] * 6, nature
    mon._max_hp = mon.stats["hp"]
    mon._current_hp = round(mon.max_hp * hp)
    mon._ability, mon._item, mon._active_turns = ability, item, 1
    if tera:
        mon._terastallized_type = tera
    if not hp:
        mon.status = Status.FNT
    return mon


def cinccino(hp=1.0):
    return pokemon("cinccino", ("bulletseed", "tailslap", "rockblast", "uturn"),
                   [4, 252, 0, 0, 0, 252], "jolly", "technician", "choiceband", hp)


def mimikyu(hp=1.0):
    return pokemon("mimikyu", ("playrough", "shadowclaw", "shadowsneak", "swordsdance"),
                   [0, 252, 0, 0, 4, 252], "jolly", "disguise", "lifeorb", hp,
                   PokemonType.GHOST)


def cloyster(hp=1.0, smashed=False):
    mon = pokemon("cloyster", ("iciclespear", "rockblast", "liquidation", "shellsmash"),
                  [4, 252, 0, 0, 0, 252], "adamant", "skilllink", "", hp)
    if smashed:
        mon.boosts.update(atk=2, spa=2, spe=2, **{"def": -1, "spd": -1})
    return mon


def mudsdale(hp=1.0):
    # The old bot used this RU marginal spread during the user's actual match.
    return pokemon("mudsdale", ("bodypress", "heavyslam", "earthquake", "stealthrock"),
                   [184, 88, 236, 0, 0, 0], "brave", "stamina", "leftovers", hp)


def battle(own, foe, bench=(), **changes):
    result = dict(gen=9, active_pokemon=own, opponent_active_pokemon=foe,
                  available_moves=list(own.moves.values()), available_switches=list(bench),
                  force_switch=False, trapped=False, can_tera=False,
                  team={f"p1: {i}": mon for i, mon in enumerate((own, *bench))},
                  opponent_team={"p2: Foe": foe}, side_conditions={},
                  opponent_side_conditions={}, fields={}, weather={}, turn=9,
                  battle_tag="battle-regression", finished=False,
                  player_role="p1")
    result.update(changes)
    return SimpleNamespace(**result)


def test_real_team_fixture_matches_replay_hp_and_speed():
    own = cinccino()
    assert own.stats["hp"] == 292
    assert own.stats["spe"] == 361
    assert mimikyu().stats["hp"] == 251
    assert cloyster().stats["hp"] == 242


def test_skill_link_damage_accounts_for_all_five_hits():
    """T8/9: Skill Link Cloyster always hits five times if the target survives."""
    attacker, defender = cloyster(smashed=True), cinccino()
    state = battle(defender, attacker)
    policy = DecisionEngine()
    skill_link = policy._damage(attacker, defender, attacker.moves["iciclespear"], state)
    attacker._ability = "shellarmor"
    policy._reset()
    ordinary = policy._damage(attacker, defender, attacker.moves["iciclespear"], state)
    assert skill_link.expected > ordinary.expected * 1.35
    assert skill_link.low > ordinary.low


def test_stamina_reduces_each_following_physical_hit():
    """T1: treating Bullet Seed as one large hit ignores repeated Defense boosts."""
    attacker, defender = cinccino(), mudsdale()
    state = battle(attacker, defender)
    policy = DecisionEngine()
    stamina = policy._damage(attacker, defender, attacker.moves["bulletseed"], state)
    defender._ability = "innerfocus"
    policy._reset()
    without_stamina = policy._damage(attacker, defender, attacker.moves["bulletseed"], state)
    assert stamina.expected < without_stamina.expected * 0.8
    assert defender.boosts["def"] == 0, "Estimation must not mutate the live battle."


def test_random_two_to_five_hits_never_claim_guaranteed_three_hit_ko():
    """T7 did only two hits; the old estimator substituted 3.5 guaranteed hits."""
    attacker, defender = cinccino(), cloyster(hp=0.70)
    policy = DecisionEngine()
    predicted = policy._damage(attacker, defender, attacker.moves["bulletseed"],
                               battle(attacker, defender))
    assert predicted.low < defender.current_hp_fraction < predicted.high
    assert 0.0 < predicted.ko_chance(defender.current_hp_fraction) < 1.0


def test_intact_disguise_blocks_single_hit_but_not_complete_multihit():
    defender, attacker = mimikyu(), mudsdale()
    policy = DecisionEngine()
    state = battle(defender, attacker)
    blocked = policy._damage(attacker, defender, attacker.moves["earthquake"], state)
    assert blocked.high <= 0.13, "Intact Disguise costs only 1/8 max HP."
    defender._ability = ""
    policy._reset()
    unblocked = policy._damage(attacker, defender, attacker.moves["earthquake"], state)
    assert unblocked.expected > blocked.expected * 2


def test_forced_replacement_uses_priority_against_faster_shell_smash_sweeper():
    """Near T9 endgame: Shadow Sneak guarantees KO; Cinccino dies before acting."""
    dead, attacker = cloyster(hp=0), cloyster(hp=0.20, smashed=True)
    fast_but_too_slow, priority = cinccino(hp=256 / 292), mimikyu()
    state = battle(dead, attacker, (fast_but_too_slow, priority), force_switch=True,
                   side_conditions={SideCondition.STEALTH_ROCK: 1})
    policy = DecisionEngine(allow_tera=False)
    chosen = policy.choose(state)
    assert chosen.action is priority
    # Entering is a free replacement, then the next decision has lethal priority.
    priority._current_hp -= priority.max_hp // 8
    state.active_pokemon = priority
    state.available_moves = list(priority.moves.values())
    state.available_switches = [fast_but_too_slow]
    state.force_switch = False
    assert policy.choose(state).action.id == "shadowsneak"


def test_normal_tera_boomburst_secures_ko_in_real_turn_four_position():
    own = pokemon("toxtricity", ("boomburst", "sludgewave", "overdrive", "shiftgear"),
                  [0, 0, 0, 252, 4, 252], "modest", "punkrock", "throatspray",
                  255 / 291, PokemonType.NORMAL)
    foe = mudsdale(hp=0.84)
    foe.boosts["def"] = 2
    state = battle(own, foe, can_tera=True)
    chosen = DecisionEngine().choose(state)
    assert chosen.action.id == "boomburst"
    assert chosen.terastallize
    assert not own.is_terastallized


def test_tera_options_survive_real_showdown_request_without_preview_object():
    """poke-env exposes can_tera but does not copy request teraType into Pokemon."""
    state = Battle("battle-gen9ru-request", "Bot", logging.getLogger("tera-regression"), gen=9)
    state._turn = 4
    request_moves = [
        {"move": name, "id": move, "pp": 16, "maxpp": 16, "target": target, "disabled": False}
        for name, move, target in (("Boomburst", "boomburst", "allAdjacent"),
                                   ("Sludge Wave", "sludgewave", "allAdjacent"),
                                   ("Overdrive", "overdrive", "allAdjacentFoes"),
                                   ("Shift Gear", "shiftgear", "self"))]
    state.parse_request({"rqid": 4, "side": {"id": "p1", "name": "Bot", "pokemon": [{
        "ident": "p1: abusa4", "details": "Toxtricity, M", "condition": "255/291",
        "active": True, "stats": {"atk": 184, "def": 176, "spa": 359, "spd": 177, "spe": 249},
        "moves": [move["id"] for move in request_moves], "baseAbility": "punkrock",
        "ability": "punkrock", "item": "throatspray", "teraType": "Normal", "terastallized": ""}]},
        "active": [{"moves": request_moves, "canTerastallize": "Normal"}]})
    state.switch("p2a: Mudsdale", "Mudsdale, F", "84/100")
    assert state.can_tera
    ranked = DecisionEngine().rank_actions(state)
    assert any(candidate.terastallize and candidate.action.id == "boomburst" for candidate in ranked)


def test_same_turn_forced_faint_request_preserves_resolved_loss_for_learning():
    """T3/4/5/9/10 deaths were all discarded before their replacement request."""
    own, foe = cinccino(), cloyster(smashed=True)
    state = battle(own, foe, turn=10)
    trace = BattleTrace()
    chosen = Candidate(own.moves["tailslap"], label="tailslap")
    trace.begin(state, [chosen], chosen, snapshot(state))
    trace.observe([["", "move", "p2a: Foe", "Icicle Spear", "p1a: 0"],
                   ["", "-damage", "p1a: 0", "0 fnt"],
                   ["", "faint", "p1a: 0"]], "Bot", "p1")
    own._current_hp = 0
    own.status = Status.FNT
    state.force_switch = True
    trace.close(state)
    outcome = trace.decisions[0]["local_outcome"]
    assert outcome["own_ko"]
    assert outcome["reliable"], "A resolved faint at turn10 is not an incomplete choice."


def test_unresolved_same_turn_request_still_cannot_train():
    own, foe = cinccino(), mudsdale()
    state = battle(own, foe)
    trace = BattleTrace()
    chosen = Candidate(own.moves["bulletseed"], label="bulletseed")
    trace.begin(state, [chosen], chosen, snapshot(state))
    trace.close(state)
    assert not trace.decisions[0]["local_outcome"]["reliable"]
