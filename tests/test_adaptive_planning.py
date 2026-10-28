"""Coherent hidden builds and contextual inference, without account traffic."""
import copy

from poke_env.battle import Pokemon, Move

from unaware.engine import Candidate, DecisionEngine
from unaware.meta import MetaSnapshot, MetaStore
from unaware.progress import transition_evidence, rerank_for_progress
from unaware.telemetry import snapshot
from test_decision_regressions import battle, pokemon
from test_search_strategy import search, worker, payload


def test_published_scarf_build_keeps_coverage_speed_and_ability_together(search):
    meta = MetaSnapshot('gen9ou', '2026-09', 0, 'public', {'enamorus': {
        'Items': {'Choice Scarf': 80, 'Choice Specs': 20},
        'Moves': {'Moonblast': 90, 'Earth Power': 80, 'Mystical Fire': 70, 'Healing Wish': 60}}})
    search.meta = meta
    foe = Pokemon(9, species='enamorus')
    data = search._set(foe, DecisionEngine(meta), opponent=True)
    assert data['setSource'] == 'published-build-hypothesis'
    assert data['set']['item'] == 'choicescarf' and data['set']['ability'] == 'contrary'
    assert data['set']['evs']['spe'] == 252
    assert set(data['set']['moves']) == {'moonblast', 'earthpower', 'mysticalfire', 'healingwish'}
    foe.moves['raindance'] = Move('raindance', gen=9)
    rejected = search._set(foe, DecisionEngine(meta), opponent=True)
    assert rejected['setSource'] != 'published-build-hypothesis'
    assert 'raindance' in rejected['set']['moves']
    search.meta = None


def test_one_weak_reply_cannot_eliminate_hidden_coverage_but_repeated_declines_reduce_it(search):
    own = pokemon('mimikyubusted', ('shadowclaw', 'playrough'), [0,252,0,0,4,252], 'jolly', 'disguise', hp=.3)
    foe = pokemon('mew', ('tackle',), [0,0,0,252,4,252], 'timid', 'synchronize')
    state = battle(own, foe)
    req = payload(search, state)
    inferred = copy.deepcopy(req['opponents'][0])
    inferred[0]['set']['moves'] = ['tackle', 'flashcannon', 'splash', 'reflect']
    passive = copy.deepcopy(inferred)
    passive[0]['set']['moves'] = ['tackle', 'recover', 'splash', 'reflect']
    req['opponents'] = [inferred, passive]
    view = snapshot(state)
    choice = {'move': 'tackle', 'target': view['own_roster'][view['active_id']],
              'attacker': view['foe_roster'][view['foe_id']], 'target_hp': .3, 'attacker_hp': 1,
              'weather': [], 'fields': [], 'conditions': [{}, {}], 'turn': 3}
    req['feedback'] = {'observed_choices': [choice]}
    one = search._rpc(req)
    assert set(one['diagnostics']['conditioned_hypotheses']) == {0, 1}
    req['feedback']['observed_choices'] = [choice] * 3
    repeated = search._rpc(req)
    assert repeated['diagnostics']['conditioned_hypotheses'] == [1]
    assert repeated['diagnostics']['observed_choice_samples'] == 3
    # A revealed threat in every surviving build remains possible regardless
    # of prior ineffective replies: this inference is not a move prohibition.
    req['opponents'][1] = copy.deepcopy(inferred)
    revealed = search._rpc(req)
    assert set(revealed['diagnostics']['conditioned_hypotheses']) == {0, 1}


def test_setup_ko_is_immediate_memory_with_hp_pp_and_context_guards():
    own = pokemon('venusaur', ('growth', 'gigadrain'), [0,0,0,252,4,252], 'timid', 'chlorophyll', hp=.25)
    foe = pokemon('slowking', ('flamethrower', 'trickroom'), [252,0,0,252,4,0], 'modest', 'regenerator')
    state = battle(own, foe)
    before = snapshot(state)
    after = copy.deepcopy(before)
    after['active_hp'] = 0
    row = {'before': before, 'after': after, 'turn': 7, 'action_key': 'move:growth',
           'observations': {'resolved_action': True, 'last_move_own': 'growth', 'last_move_foe': 'flamethrower',
                            'own_fainted': True, 'damage_events': [{'own': True, 'residual': False, 'move': 'flamethrower'}]}}
    facts = transition_evidence(row, [])
    assert len(facts) == 1 and facts[0]['kind'] == 'setup_ko'
    def options():
        return [Candidate(own.moves['growth'], score=3), Candidate(own.moves['gigadrain'], score=1)]
    assert rerank_for_progress(before, options(), [], facts)[0].action.id == 'gigadrain'
    healed = copy.deepcopy(before)
    healed['active_hp'] = .9
    assert rerank_for_progress(healed, options(), [], facts)[0].action.id == 'growth'
    depleted = copy.deepcopy(before)
    depleted['foe_roster'][depleted['foe_id']]['moves']['flamethrower']['pp'] = 0
    assert rerank_for_progress(depleted, options(), [], facts)[0].action.id == 'growth'
    row['observations']['critical'] = True
    assert transition_evidence(row, []) == []


def test_legacy_meta_cut_is_preserved_when_first_new_population_is_saved(tmp_path):
    store = MetaStore(tmp_path)
    def data(cut):
        return MetaSnapshot('gen9ru', '2026-09', cut,
            f'https://www.smogon.com/stats/2026-09/chaos/gen9ru-{cut}.json', {'mimikyu': {'Moves': {'Play Rough': 1}}})
    store._save(data(1695))
    (tmp_path / 'gen9ru-1695.json').unlink()
    store._save(data(0), 0)
    assert store.load('gen9ru', 1695).rating == 1695
    assert store.load('gen9ru', 0).rating == 0
