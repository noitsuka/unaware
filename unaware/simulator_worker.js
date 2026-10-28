'use strict';
// Official Showdown simulation, local JSONL only. Never connects to a server.
// Battle.fromJSON clones a fully reconstructed scenario before every branch.
const path = require('path');
const readline = require('readline');
const {isMainThread, parentPort} = require('worker_threads');
let Showdown;
for (const location of [path.join(__dirname, '..', 'runtime', 'node_modules', 'pokemon-showdown'),
  process.env.SHOWDOWN_SIMULATOR_PATH, 'pokemon-showdown'].filter(Boolean)) {
  try { Showdown = require(location); break; } catch (_) {}
}
if (!Showdown) {
  process.stdout.write(JSON.stringify({ready: false, error: 'Simulator runtime missing. Run start.bat to install it.'}) + '\n');
  process.exit(1);
}
const {Battle, Dex, PRNG, toID} = Showdown;
const clone = state => Battle.fromJSON(structuredClone(state));
const cap = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
const alive = mon => mon && !mon.fainted && mon.hp > 0;
const fractions = side => Object.fromEntries(side.pokemon.map(mon => [mon.name, mon.hp / mon.maxhp]));
const baseSpeciesID = species => toID(Dex.species.get(species).baseSpecies || species);

function strategyFor(profile, species) {
  if (!profile) return null;
  const key = baseSpeciesID(species);
  return {archetype: profile.archetype,
    roles: profile.roles?.find(row => baseSpeciesID(row.species) === key)?.roles || [],
    winCondition: profile.win_conditions?.some(value => baseSpeciesID(value) === key) || false};
}

function effectState(battle, id, target, source, extra = {}) {
  return battle.initEffectState({id, target, source, sourceSlot: source?.getSlot(), ...extra});
}

function patchPokemon(mon, data, battle, active, scenario, warnings) {
  mon.maxhp = data.actualMaxHp || data.stats?.hp || mon.maxhp;
  mon.baseMaxhp = mon.maxhp;
  mon.hp = data.fainted ? 0 : (data.actualHp ?? Math.max(1, Math.round(mon.maxhp * data.hp)));
  mon.hp = cap(mon.hp, 0, mon.maxhp);
  mon.fainted = !mon.hp;
  mon.faintQueued = false;
  for (const [stat, value] of Object.entries(data.stats || {})) {
    if (stat !== 'hp' && Number.isFinite(value)) {
      mon.storedStats[stat] = value;
      mon.baseStoredStats[stat] = value;
    }
  }
  mon.boosts = {atk: 0, def: 0, spa: 0, spd: 0, spe: 0, accuracy: 0, evasion: 0, ...(data.boosts || {})};
  mon.status = data.status === 'fnt' ? '' : data.status;
  mon.statusState = effectState(battle, mon.status, mon, mon);
  if (mon.status === 'tox') mon.statusState.stage = cap(data.statusCounter || 0, 0, 15);
  if (mon.status === 'slp') {
    // Actual sleep duration is hidden. All scenarios use a plausible remaining
    // duration conditioned on the observed count; it is not reconstructed fact.
    const total = 2 + scenario % 3;
    mon.statusState.startTime = total;
    mon.statusState.time = Math.max(1, total - (data.statusCounter || 0));
    warnings.add('Hidden sleep, effect and weather item durations are guesses.');
  }
  mon.activeTurns = active ? Math.max(0, data.activeTurns ?? 1) : 0;
  // Fake Out and First Impression count attempted move actions, not turns.
  // poke-env exposes elapsed active turns; absent a tracked action count this
  // conservative reconstruction prevents both moves from working repeatedly.
  mon.activeMoveActions = active ? Math.max(0, data.activeMoveActions ?? mon.activeTurns - 1) : 0;
  mon.newlySwitched = active && !mon.activeTurns;
  mon.isActive = active;
  mon.switchFlag = false;
  mon.forceSwitchFlag = false;
  mon.skipBeforeSwitchOutEventFlag = false;
  mon.lastMove = data.lastMove ? battle.dex.moves.get(data.lastMove) : null;
  mon.lastMoveUsed = mon.lastMove;
  mon.lastMoveTargetLoc = 1;
  mon.volatiles = {};
  mon.abilityState = effectState(battle, mon.ability, mon, mon, {started: active});
  mon.itemState = effectState(battle, mon.item, mon, mon, {started: active});
  mon.types = data.terastallized ? mon.baseSpecies.types : (data.types?.length ? data.types : mon.baseSpecies.types);
  mon.baseTypes = mon.baseSpecies.types;
  mon.terastallized = data.terastallized || undefined;
  mon.canTerastallize = data.terastallized ? null : mon.teraType;
  for (const slot of mon.moveSlots) {
    if (data.pp && Number.isFinite(data.pp[slot.id])) slot.pp = cap(data.pp[slot.id], 0, slot.maxpp);
    slot.disabled = false;
    slot.disabledSource = '';
  }
  const foe = mon.side.foe.active[0];
  for (const [observedId, counter] of Object.entries(data.effects || {})) {
    // These are public annotations of an ability, not independent volatiles.
    if (observedId === 'supremeoverlord' || /^fallen[0-5]$/.test(observedId)) continue;
    const paradox = /^(protosynthesis|quarkdrive)(atk|def|spa|spd|spe)$/.exec(observedId);
    const id = paradox ? paradox[1] : observedId;
    const condition = battle.dex.conditions.get(id);
    if (!condition.exists) { warnings.add(`Effect not rebuilt: ${id}`); continue; }
    const self = ['aquaring', 'ingrain', 'focusenergy', 'substitute', 'protosynthesis', 'quarkdrive', 'charge'].includes(id);
    const state = effectState(battle, id, mon, self ? mon : foe, {});
    if (condition.duration) state.duration = Math.max(1, condition.duration - (counter || 0));
    if (id === 'substitute') {
      state.hp = Math.floor(mon.maxhp / 4);
      warnings.add('Substitute HP is estimated when the protocol does not show it.');
    }
    if (id === 'encore' || id === 'disable') {
      if (!data.lastMove) { warnings.add(`Alvo desconhecido de ${id}`); continue; }
      state.move = data.lastMove;
    }
    if (id === 'confusion') state.time = Math.max(1, 3 - (counter || 0));
    if (id === 'protosynthesis' || id === 'quarkdrive') {
      state.bestStat = paradox ? paradox[2] : mon.getBestStat(false, true);
    }
    if (id === 'partiallytrapped') {
      state.sourceEffect = battle.dex.moves.get('bind');
      state.duration = Math.max(1, 5 - (counter || 0));
    }
    mon.volatiles[id] = state;
  }
  if (data.protectCounter) mon.volatiles.stall = effectState(battle, 'stall', mon, mon, {counter: 3 ** data.protectCounter, duration: 2});
  if (data.mustRecharge) mon.volatiles.mustrecharge = effectState(battle, 'mustrecharge', mon, mon, {duration: 2});
  if (data.preparingMove) {
    mon.volatiles.twoturnmove = effectState(battle, 'twoturnmove', mon, foe, {move: data.preparingMove, targetLoc: 1, duration: 2});
    mon.volatiles[data.preparingMove] = effectState(battle, data.preparingMove, mon, foe, {duration: 2});
  }
  if (mon.getItem().isChoice && data.lastMove && active && mon.activeTurns > 0) {
    mon.volatiles.choicelock = effectState(battle, 'choicelock', mon, mon, {move: data.lastMove, sourceEffect: mon.getItem()});
  }
}

function reconstruct(payload, hypothesis = 0, seed = 0, warnings = new Set()) {
  const teams = [payload.own, payload.opponents[hypothesis] || payload.opponents[0]];
  const battle = new Battle({formatid: 'gen9customgame', seed: [31, 41 + seed, 59 + hypothesis, 26],
    p1: {name: 'Bot', team: teams[0].map((data, i) => ({...data.set, searchPriors: data.priors, searchRevealedMoves: data.revealedMoves,
      searchStrategy: strategyFor(payload.strategy, data.set.species), name: `own${i}`}))},
    p2: {name: 'Opponent', team: teams[1].map((data, i) => ({...data.set, searchPriors: data.priors, searchRevealedMoves: data.revealedMoves, name: `foe${i}`}))},
    strictChoices: false, debug: false});
  if (battle.requestState === 'teampreview') battle.makeChoices('team 123456'.slice(0, 5 + teams[0].length), 'team 123456'.slice(0, 5 + teams[1].length));
  if (payload.preview) {
    // A preview starts an actual fresh battle. Preserve Showdown's initial
    // Intimidate, entry abilities, Booster Energy, weather and terrain events;
    // observed midbattle reconstruction below intentionally overrides them.
    for (let sideIndex = 0; sideIndex < 2; sideIndex++) {
      const side = battle.sides[sideIndex];
      const canTera = sideIndex ? payload.opponentCanTera : payload.canTera;
      for (const mon of side.pokemon) if (!canTera) mon.canTerastallize = null;
    }
    battle.log = [];
    battle.inputLog = [];
    battle.sentLogPos = 0;
    battle.makeRequest('move');
    return battle;
  }
  battle.turn = payload.turn || 1;
  battle.log = [];
  battle.inputLog = [];
  battle.sentLogPos = 0;
  battle.faintQueue = [];
  battle.queue.clear();
  battle.midTurn = false;
  battle.ended = false;
  battle.winner = '';
  battle.field.weather = '';
  battle.field.weatherState = effectState(battle, '', battle.field);
  battle.field.terrain = '';
  battle.field.terrainState = effectState(battle, '', battle.field);
  battle.field.pseudoWeather = {};
  for (let sideIndex = 0; sideIndex < 2; sideIndex++) {
    const side = battle.sides[sideIndex];
    for (const [i, mon] of side.pokemon.entries()) patchPokemon(mon, teams[sideIndex][i], battle, i === 0, hypothesis, warnings);
    side.pokemonLeft = side.pokemon.filter(alive).length;
    side.totalFainted = side.pokemon.length - side.pokemonLeft;
    for (const mon of side.pokemon) {
      // The onStart event happened before observed fainted teammates were
      // restored. Preserve the actual known per-battle Supreme Overlord bonus.
      if (mon.ability === 'supremeoverlord') mon.abilityState.fallen = cap(side.totalFainted, 0, 5);
    }
    side.faintedLastTurn = null;
    side.faintedThisTurn = null;
    side.sideConditions = {};
    side.slotConditions = [{}];
    const canTera = sideIndex ? payload.opponentCanTera : payload.canTera;
    for (const mon of side.pokemon) mon.canTerastallize = canTera && !mon.terastallized ? mon.teraType : null;
    for (const [id, value] of Object.entries(payload.sideConditions?.[sideIndex] || {})) {
      const condition = battle.dex.conditions.get(id);
      if (!condition.exists) { warnings.add(`Side condition not rebuilt: ${id}`); continue; }
      const state = effectState(battle, id, side, side.active[0]);
      if (id === 'spikes' || id === 'toxicspikes') state.layers = value || 1;
      if (condition.duration) state.duration = Math.max(1, condition.duration - (battle.turn - (value || battle.turn)));
      if (['reflect', 'lightscreen', 'auroraveil'].includes(id)) {
        const duration = side.pokemon.some(mon => mon.item === 'lightclay') ? 8 : 5;
        state.duration = Math.max(1, duration - (battle.turn - (value || battle.turn)));
      }
      side.sideConditions[id] = state;
    }
  }
  for (const [id0, startTurn] of Object.entries(payload.weather || {})) {
    const id = id0 === 'snow' ? 'snowscape' : id0;
    const condition = battle.dex.conditions.get(id);
    if (!condition.exists) continue;
    const setter = battle.getAllPokemon().find(mon => (
      (id === 'raindance' && ['drizzle', 'primordialsea'].includes(mon.ability)) ||
      (id === 'sunnyday' && ['drought', 'orichalcumpulse', 'desolateland'].includes(mon.ability)) ||
      (id === 'sandstorm' && mon.ability === 'sandstream') ||
      (id === 'snowscape' && mon.ability === 'snowwarning')));
    const source = setter || battle.sides[0].active[0];
    const state = effectState(battle, id, battle.field, source);
    if (!['desolateland', 'primordialsea', 'deltastream'].includes(id)) {
      const duration = ['damprock', 'heatrock', 'smoothrock', 'icyrock'].includes(source.item) ? 8 : 5;
      state.duration = Math.max(1, duration - (battle.turn - (startTurn || battle.turn)));
    }
    battle.field.weather = id;
    battle.field.weatherState = state;
  }
  for (const [id, startTurn] of Object.entries(payload.fields || {})) {
    const condition = battle.dex.conditions.get(id);
    if (!condition.exists) { warnings.add(`Field effect not rebuilt: ${id}`); continue; }
    const state = effectState(battle, id, battle.field, battle.sides[0].active[0]);
    if (condition.duration) state.duration = Math.max(1, condition.duration - (battle.turn - (startTurn || battle.turn)));
    if (id.endsWith('terrain')) {
      battle.field.terrain = id;
      battle.field.terrainState = state;
    } else battle.field.pseudoWeather[id] = state;
  }
  for (const mon of battle.getAllPokemon()) {
    // A revealed Paradox boost surviving without its activating weather or
    // terrain must have come from Booster Energy. Retain it after reconstruction.
    if (mon.volatiles.protosynthesis && battle.field.weather !== 'sunnyday') mon.volatiles.protosynthesis.fromBooster = true;
    if (mon.volatiles.quarkdrive && battle.field.terrain !== 'electricterrain') mon.volatiles.quarkdrive.fromBooster = true;
  }
  // The server request is authoritative about current legality. All original
  // move slots are retained, so moving forward can clear Taunt/Choice effects.
  const active = battle.sides[0].active[0];
  const available = new Map((payload.available || []).map(move => [toID(move.id || move.move), move]));
  for (const slot of active.moveSlots) {
    const supplied = available.get(slot.id);
    if (supplied && supplied.pp !== undefined) slot.pp = supplied.pp;
    slot.disabled = !supplied || !!supplied.disabled;
    slot.disabledSource = slot.disabled ? 'Server request' : '';
  }
  active.trapped = !!payload.trapped;
  if (payload.forceSwitch) {
    active.switchFlag = true;
    // A faint/pivot replacement resumes a paused request, not an attack turn.
    battle.midTurn = true;
    battle.makeRequest('switch');
  } else battle.makeRequest('move');
  return battle;
}

function damage(battle, attacker, defender, moveId) {
  if (!alive(attacker) || !alive(defender)) return 0;
  const move = battle.dex.getActiveMove(moveId);
  if (move.category === 'Status') return 0;
  const previousSeed = battle.prng.getSeed();
  const context = [battle.activeMove, battle.activePokemon, battle.activeTarget];
  // Showdown counts nested events and throws "Stack overflow" past 8. An
  // estimate that fails mid-event must not leak that depth into later calls.
  const depth = battle.eventDepth;
  let value = 0;
  let shield = false;
  try {
    battle.setActiveMove(move, attacker, defender);
    battle.singleEvent('ModifyMove', move, null, attacker, defender, move);
    battle.runEvent('ModifyMove', attacker, defender, move, move);
    // getDamage is downstream of the official hit/immunity gate. Calling it
    // alone assigned pressure to Normal vs Ghost and Ground vs Levitate.
    if (!defender.runImmunity(move, false)) return 0;
    // These official TryHit ability gates absorb a move before getDamage.
    // Do not execute their healing/boost events merely to order a search beam.
    // Check both temporary suppression and the active move's Mold Breaker
    // context. This projection must never heal or boost either Pokémon.
    const absorbers = {Water: ['waterabsorb', 'stormdrain', 'dryskin'],
      Electric: ['voltabsorb', 'lightningrod', 'motordrive'],
      Fire: ['flashfire', 'wellbakedbody'], Grass: ['sapsipper'], Ground: ['eartheater']};
    const abilityApplies = abilities => defender.hasAbility(abilities) && !battle.suppressingAbility(defender);
    if (abilityApplies(absorbers[move.type] || [])) return 0;
    if (move.flags.bullet && abilityApplies('bulletproof') ||
        move.flags.sound && abilityApplies('soundproof') ||
        move.flags.wind && abilityApplies('windrider')) return 0;
    if (move.id !== 'struggle' && abilityApplies('wonderguard') && defender.runEffectiveness(move) <= 0) return 0;
    value = battle.actions.getDamage(attacker, defender, move, true) || 0;
    shield = !defender.volatiles.substitute && (
      abilityApplies('disguise') && ['mimikyu', 'mimikyutotem'].includes(defender.species.id) ||
      abilityApplies('iceface') && defender.species.id === 'eiscue' && move.category === 'Physical');
  } catch (_) {} finally {
    battle.eventDepth = depth;
    battle.setActiveMove(...context);
    battle.prng = new PRNG(previousSeed);
  }
  const hits = move.multihit ? (Array.isArray(move.multihit) ? (attacker.hasAbility('skilllink') ? move.multihit[1] : 3) : move.multihit) : 1;
  const accuracy = typeof move.accuracy === 'number' ? move.accuracy / 100 : 1;
  if (shield) return (value * Math.max(0, hits - 1) + (defender.hasAbility('disguise') ? defender.maxhp / 8 : 0)) * accuracy;
  return value * hits * accuracy;
}

function priorityOf(battle, attacker, defender, move) {
  const activeMove = battle.dex.getActiveMove(move.id);
  const depth = battle.eventDepth;
  try {
    let priority = battle.singleEvent('ModifyPriority', activeMove, null, attacker, defender, null, activeMove.priority);
    priority = battle.runEvent('ModifyPriority', attacker, defender, activeMove, priority);
    return priority ?? activeMove.priority;
  } catch (_) {
    return activeMove.priority;
  } finally {
    battle.eventDepth = depth;
  }
}

function strongest(battle, attacker, defender) {
  if (!alive(attacker) || !alive(defender)) return {damage: 0, priority: 0};
  let best = {damage: 0, priority: 0};
  for (const slot of attacker.moveSlots) {
    if (slot.pp <= 0 || slot.disabled) continue;
    if (attacker.volatiles.choicelock?.move && attacker.volatiles.choicelock.move !== slot.id) continue;
    if (['fakeout', 'firstimpression'].includes(slot.id) && attacker.activeMoveActions >= 1) continue;
    const move = battle.dex.moves.get(slot.id);
    const value = damage(battle, attacker, defender, move.id);
    const priority = priorityOf(battle, attacker, defender, move);
    if (value > best.damage) best = {damage: value, priority};
  }
  return best;
}

function usableAttack(battle, attacker, defender, threat) {
  let best = {damage: 0, priority: 0};
  let speedFirst = attacker.getActionSpeed() >= defender.getActionSpeed();
  if (battle.field.pseudoWeather.trickroom) speedFirst = !speedFirst;
  for (const slot of attacker.moveSlots) {
    if (slot.pp <= 0 || slot.disabled || attacker.volatiles.choicelock?.move && attacker.volatiles.choicelock.move !== slot.id) continue;
    if (['fakeout', 'firstimpression'].includes(slot.id) && attacker.activeMoveActions >= 1) continue;
    const move = battle.dex.moves.get(slot.id), priority = priorityOf(battle, attacker, defender, move);
    const first = priority > threat.priority || priority === threat.priority && speedFirst;
    if (!first && threat.damage >= attacker.hp) continue;
    const value = damage(battle, attacker, defender, move.id);
    if (value > best.damage) best = {damage: value, priority};
  }
  return best;
}

// What holding the item is worth for the rest of the game. Without it the
// search saw Knock Off on a Leftovers/Eviolite/Boots wall as plain damage
// (ladder: chosen in 7% of the turns it was available, while defensive cores
// were our worst matchups). Signature masks cannot be removed: no value.
const ITEM_VALUE = {
  leftovers: 7, eviolite: 14, assaultvest: 6, rockyhelmet: 4, heavydutyboots: 3,
  choiceband: 6, choicespecs: 6, choicescarf: 6, lifeorb: 5, expertbelt: 3,
  sitrusberry: 3, lumberry: 3, boosterenergy: 5, airballoon: 4, weaknesspolicy: 3,
  covertcloak: 3, clearamulet: 2, loadeddice: 3, lightclay: 3, whiteherb: 2,
  mirrorherb: 2, powerherb: 2, redcard: 2, ejectbutton: 2, ejectpack: 2,
  shedshell: 2, protectivepads: 2, throatspray: 2,
};

function itemValue(mon) {
  const item = mon.item;
  if (!item || /mask$/.test(item)) return 0;
  // Items that hurt the wrong holder are negative, so Trick keeps its value.
  if (item === 'blacksludge') return mon.hasType('Poison') ? 7 : -5;
  if (item === 'toxicorb') return mon.hasAbility(['poisonheal', 'guts', 'toxicboost', 'quickfeet']) ? 10 : -6;
  if (item === 'flameorb') return mon.hasAbility(['guts', 'flareboost', 'quickfeet']) ? 8 : -6;
  if (item === 'focussash') return mon.hp === mon.maxhp ? 8 : 0;
  if (['choiceband', 'choicespecs', 'choicescarf'].includes(item)) {
    const status = mon.moveSlots.filter(slot => mon.battle.dex.moves.get(slot.id).category === 'Status').length;
    return status >= 2 ? -8 : 6;
  }
  return ITEM_VALUE[item] ?? 2;
}

function sideValue(battle, side) {
  let score = 0;
  for (const mon of side.pokemon) {
    if (!alive(mon)) continue;
    score += itemValue(mon);
    const hp = mon.hp / mon.maxhp;
    const plan = mon.set.searchStrategy;
    const roles = plan?.roles || [];
    // A designated disposable lead and a wall anchoring a defensive core
    // have different future resource value. Exact mechanics still determine
    // whether any sacrifice actually creates usable field pressure.
    const disposable = plan?.archetype === 'hyper_offense' && roles.includes('suicide_lead');
    score += (disposable ? 45 : 90) + (disposable ? 35 : 60) * Math.sqrt(hp);
    if (plan?.winCondition && !disposable) score += 5 * Math.sqrt(hp);
    if (['stall', 'semi_stall'].includes(plan?.archetype) && roles.includes('wall')) score += 6 * Math.sqrt(hp);
    const statusPenalty = {tox: 13 + 2 * (mon.statusState.stage || 0), psn: 10, par: 13, slp: 18, frz: 23};
    score -= statusPenalty[mon.status] || 0;
    if (mon.status === 'brn' && !mon.hasAbility('guts')) {
      const physical = mon.moveSlots.some(slot => battle.dex.moves.get(slot.id).category === 'Physical');
      score -= physical ? 22 : 7;
    }
    if (mon.isActive) {
      // Value setup only while the Pokémon survives and can exploit it.
      const boosts = mon.boosts;
      const usedStats = new Set(mon.moveSlots.map(slot => battle.dex.moves.get(slot.id)).filter(move =>
        move.category !== 'Status' && move.overrideOffensivePokemon !== 'target').map(move =>
        move.overrideOffensiveStat || (move.category === 'Physical' ? 'atk' : 'spa')));
      const attack = usedStats.size ? Math.max(...[...usedStats].map(stat => boosts[stat] || 0)) : 0;
      let opportunity = 1;
      const target = side.foe.active[0];
      if ((attack > 0 || boosts.spe > 0) && alive(target)) {
        const outgoing = strongest(battle, mon, target), incoming = strongest(battle, target, mon);
        let first = mon.getActionSpeed() >= target.getActionSpeed();
        if (battle.field.pseudoWeather.trickroom) first = !first;
        first = outgoing.priority > incoming.priority || outgoing.priority === incoming.priority && first;
        // A boost has no immediate sweep value if the active is about to die
        // before dealing useful damage. Priority and an actual finishing hit
        // retain it. This prevents setup followed by compulsory retreat from
        // being rewarded merely for producing a positive stat stage.
        if (!first && incoming.damage >= mon.hp) opportunity = .05;
        else if (outgoing.damage === 0) opportunity = .15;
        else if (incoming.damage >= mon.hp && outgoing.damage < target.hp) opportunity = .45;
      }
      score += cap(attack, -6, 6) * (plan?.archetype === 'hyper_offense' ? 8 : 7) * Math.sqrt(hp) * (attack > 0 ? opportunity : 1);
      score += cap(boosts.spe, -6, 6) * 4 * Math.sqrt(hp) * (boosts.spe > 0 ? opportunity : 1);
      score += (boosts.def + boosts.spd) * 2 * Math.sqrt(hp);
      if (mon.volatiles.substitute) score += 10 * (mon.volatiles.substitute.hp / Math.max(1, mon.maxhp / 4));
      if (mon.volatiles.leechseed) score -= 12;
      if (mon.volatiles.confusion) score -= 5;
    }
  }
  const remaining = side.pokemon.filter(mon => alive(mon) && !mon.isActive);
  const conditions = side.sideConditions;
  for (const mon of remaining) {
    if (mon.hasItem('heavydutyboots') || mon.hasAbility('magicguard')) continue;
    if (conditions.stealthrock && mon.runImmunity('Rock', false)) {
      score -= 25 * 0.125 * (2 ** mon.runEffectiveness('Rock'));
    }
    if (mon.isGrounded()) {
      if (conditions.spikes) score -= 25 * [0, .125, 1 / 6, .25][cap(conditions.spikes.layers || 1, 1, 3)];
      if (conditions.toxicspikes && !mon.hasType(['Steel', 'Poison']) && !mon.status) score -= 5;
      if (conditions.stickyweb) score -= 4;
    }
  }
  for (const id of ['reflect', 'lightscreen', 'auroraveil', 'tailwind']) {
    if (conditions[id]) score += Math.min(10, (conditions[id].duration || 1) * 2);
  }
  const style = side.pokemon.find(mon => mon.set.searchStrategy)?.set.searchStrategy.archetype;
  if (style === 'hyper_offense') {
    const sweepers = side.pokemon.filter(mon => alive(mon) && mon.set.searchStrategy?.roles.includes('setup_sweeper'));
    const protectedTurns = Math.max(...['reflect', 'lightscreen', 'auroraveil'].map(id => conditions[id]?.duration || 0));
    score += Math.min(12, sweepers.length * Math.min(protectedTurns, 4));
    const vulnerable = side.foe.pokemon.filter(mon => alive(mon) && !mon.hasItem('heavydutyboots') && !mon.hasAbility('magicguard'));
    const hazards = side.foe.sideConditions;
    if (hazards.stealthrock) score += vulnerable.length * 2;
    if (hazards.spikes) score += vulnerable.filter(mon => mon.isGrounded()).length * Math.min(3, hazards.spikes.layers || 1);
    if (hazards.stickyweb) {
      // Reward the matchups that Web actually flips for the surviving builds,
      // rather than assigning the same value to every opponent or every HO.
      const attackers = side.pokemon.filter(mon => alive(mon) && !mon.set.searchStrategy?.roles.includes('suicide_lead') &&
        mon.moveSlots.some(slot => battle.dex.moves.get(slot.id).category !== 'Status'));
      let flips = 0;
      for (const foe of vulnerable) if (foe.isGrounded() && !foe.hasAbility(['clearbody', 'whitesmoke', 'fullmetalbody', 'contrary']) && !foe.hasItem('clearamulet')) {
        const speed = foe.getActionSpeed();
        flips += attackers.filter(mon => mon.getActionSpeed() < speed && mon.getActionSpeed() > speed * 2 / 3).length;
      }
      score += Math.min(22, flips * 2.5);
    }
  }
  const weather = battle.field.weather;
  const benefits = {sunnyday: ['chlorophyll', 'solarpower', 'protosynthesis', 'orichalcumpulse'],
    raindance: ['swiftswim', 'raindish', 'hydration'], sandstorm: ['sandrush', 'sandforce'],
    snowscape: ['slushrush', 'icebody']}[weather] || [];
  const turns = battle.field.weatherState.duration || 1;
  score += Math.min(15, side.pokemon.filter(mon => alive(mon) && mon.hasAbility(benefits)).length * Math.min(3, turns) * 2);
  // Preserve the one-per-battle resource, but never outweigh a secured KO.
  if (side.pokemon.some(mon => mon.canTerastallize)) score += 3;
  return score;
}

// ---- Brain v2 evaluation (3.13) ---------------------------------------------
// Symmetric material/position value modelled on Foul Play's evaluation (the
// strongest open-source Showdown bot: MCTS + this kind of evaluation, 80% GXE
// and top-100 in gen9 OU). HP is linear (chip damage on a healthy wall counts),
// a living Pokémon is worth 30 on top of its HP, status/hazards/boosts have
// fixed costs, and a spent Tera costs 75 so it is used only when it changes
// more than that. No action-specific bonuses: the search decides.
const STATUS_COST = {frz: 40, slp: 25, par: 25, tox: 30, psn: 10, brn: 25};
const BOOST_SCALE = {6: 3.3, 5: 3.15, 4: 3, 3: 2.5, 2: 2, 1: 1, 0: 0, '-1': -1, '-2': -2, '-3': -2.5, '-4': -3, '-5': -3.15, '-6': -3.3};
let BRAIN_V2 = false;

function statusCost(battle, mon) {
  const status = mon.status;
  if (!status) return 0;
  if (status === 'brn') {
    if (mon.hasAbility(['guts', 'marvelscale', 'quickfeet'])) return -2 * STATUS_COST.brn;
    const moves = mon.moveSlots.map(slot => battle.dex.moves.get(slot.id)).filter(move => move.category !== 'Status');
    const physical = moves.filter(move => move.category === 'Physical').length;
    const share = moves.length ? physical / moves.length : .5;
    return STATUS_COST.brn * (mon.storedStats.spa > mon.storedStats.atk ? share / 2 : Math.max(.3, share));
  }
  if (status === 'psn' || status === 'tox') {
    if (mon.hasAbility('poisonheal')) return -15;
    if (mon.hasAbility(['guts', 'marvelscale', 'quickfeet', 'toxicboost', 'magicguard'])) return -10;
    return STATUS_COST[status];
  }
  return STATUS_COST[status] || 0;
}

function hazardCost(mon, conditions) {
  if (mon.hasItem('heavydutyboots')) return 0;
  let cost = 0;
  if (conditions.stealthrock && !mon.hasAbility('magicguard')) cost += 10 * 2 ** mon.runEffectiveness('Rock');
  if (mon.isGrounded()) {
    if (conditions.spikes) cost += 7 * (conditions.spikes.layers || 1);
    if (conditions.toxicspikes && !mon.status && !mon.hasType(['Poison', 'Steel'])) cost += 7 * (conditions.toxicspikes.layers || 1);
    if (conditions.stickyweb) cost += 25;
  }
  return cost;
}

function sideValueV2(battle, side) {
  let score = 0;
  const conditions = side.sideConditions;
  for (const mon of side.pokemon) {
    if (!alive(mon)) continue;
    let value = 100 * mon.hp / mon.maxhp + itemValue(mon) - statusCost(battle, mon);
    if (!mon.isActive) value -= hazardCost(mon, conditions);
    score += Math.max(0, value) + 30;
  }
  const active = side.active[0];
  if (alive(active)) {
    const target = side.foe.active[0];
    for (const [stat, unit] of [['atk', 30], ['spa', 30], ['spe', 30], ['def', 15], ['spd', 15]]) {
      const stage = active.boosts[stat] || 0;
      if (!stage) continue;
      let worth = unit * BOOST_SCALE[stage];
      // A boost on a Pokémon about to be KO'd before acting is worth little:
      // the shallow horizon must not reward setting up into a faster KO.
      if (worth > 0 && ['atk', 'spa', 'spe'].includes(stat) && alive(target)) {
        const incoming = strongest(battle, target, active);
        let first = active.getActionSpeed() >= target.getActionSpeed();
        if (battle.field.pseudoWeather.trickroom) first = !first;
        if (!first && incoming.damage >= active.hp) worth *= .1;
      }
      score += worth;
    }
    if (active.volatiles.substitute) score += 75 * Math.min(1, active.volatiles.substitute.hp / Math.max(1, active.maxhp / 4));
    if (active.volatiles.leechseed) score -= 30;
    if (active.volatiles.confusion) score -= 20;
  }
  if (side.pokemon.some(mon => mon.terastallized)) score -= 75;
  for (const [id, worth] of [['reflect', 20], ['lightscreen', 20], ['auroraveil', 40], ['tailwind', 7], ['safeguard', 5]]) {
    if (conditions[id]) score += worth;
  }
  const weather = battle.field.weather;
  const benefits = {sunnyday: ['chlorophyll', 'solarpower', 'protosynthesis', 'orichalcumpulse'],
    raindance: ['swiftswim', 'raindish', 'hydration'], sandstorm: ['sandrush', 'sandforce'],
    snowscape: ['slushrush', 'icebody']}[weather] || [];
  const turns = battle.field.weatherState.duration || 1;
  score += Math.min(12, side.pokemon.filter(mon => alive(mon) && mon.hasAbility(benefits)).length * Math.min(3, turns) * 2);
  return score;
}

function evaluateV2(battle) {
  if (battle.ended) return (battle.winner === 'Bot' ? 10000 : battle.winner === 'Opponent' ? -10000 : 0) +
    .1 * (sideValueV2(battle, battle.sides[0]) - sideValueV2(battle, battle.sides[1]));
  let score = sideValueV2(battle, battle.sides[0]) - sideValueV2(battle, battle.sides[1]);
  // The horizon is one or two turns: who KOs whom first in the current
  // matchup is the one forward-looking term (Foul Play searches 10+ deep).
  const own = battle.sides[0].active[0], foe = battle.sides[1].active[0];
  if (alive(own) && alive(foe)) {
    const ownMaximum = strongest(battle, own, foe), foeMaximum = strongest(battle, foe, own);
    const outgoing = usableAttack(battle, own, foe, foeMaximum), incoming = usableAttack(battle, foe, own, ownMaximum);
    let speedOrder = own.getActionSpeed() >= foe.getActionSpeed();
    if (battle.field.pseudoWeather.trickroom) speedOrder = !speedOrder;
    const ownFirst = outgoing.priority > incoming.priority || outgoing.priority === incoming.priority && speedOrder;
    const ownPressure = cap(outgoing.damage / Math.max(1, foe.hp), 0, 1.5);
    const foePressure = cap(incoming.damage / Math.max(1, own.hp), 0, 1.5);
    score += 8 * (ownPressure - foePressure);
    if (ownFirst && ownPressure >= 1) score += 30;
    if (!ownFirst && foePressure >= 1) score -= 30;
  }
  return score;
}

function evaluate(battle) {
  if (BRAIN_V2) return evaluateV2(battle);
  // Preserve decisive results, with a tiny material tie-break among equally
  // losing lines. Immunity and useful damage cannot become identical choices.
  if (battle.ended) return (battle.winner === 'Bot' ? 10000 : battle.winner === 'Opponent' ? -10000 : 0) +
    .1 * (sideValue(battle, battle.sides[0]) - sideValue(battle, battle.sides[1]));
  let score = sideValue(battle, battle.sides[0]) - sideValue(battle, battle.sides[1]);
  const own = battle.sides[0].active[0], foe = battle.sides[1].active[0];
  if (alive(own) && alive(foe)) {
    const ownMaximum = strongest(battle, own, foe), foeMaximum = strongest(battle, foe, own);
    const outgoing = usableAttack(battle, own, foe, foeMaximum), incoming = usableAttack(battle, foe, own, ownMaximum);
    let speedOrder = own.getActionSpeed() >= foe.getActionSpeed();
    if (battle.field.pseudoWeather.trickroom) speedOrder = !speedOrder;
    const ownFirst = outgoing.priority > incoming.priority || outgoing.priority === incoming.priority && speedOrder;
    const ownPressure = cap(outgoing.damage / Math.max(1, foe.hp), 0, 1.5);
    const foePressure = cap(incoming.damage / Math.max(1, own.hp), 0, 1.5);
    score += 10 * (ownPressure - foePressure);
    if (ownFirst && ownPressure >= 1) score += 55;
    if (!ownFirst && foePressure >= 1) score -= 55;
    // A quiet switch isn't resource preservation if its recipient is unable
    // to attack before the next lethal blow. Price that imminent loss at the
    // leaf, so retreating repeatedly cannot keep six endangered resources at
    // their full future value. Real resolved KOs remain much larger costs.
    if (!outgoing.damage && foeMaximum.damage >= own.hp) score -= 35;
    if (!incoming.damage && ownMaximum.damage >= foe.hp) score += 35;
  }
  return score;
}

function actionChoice(battle, sideIndex, action) {
  const side = battle.sides[sideIndex], request = side.activeRequest;
  if (!request || request.wait) return '';
  if (action.switch !== undefined) {
    const wanted = action.name ? side.pokemon.find(mon => mon.name === action.name) : side.pokemon.find(mon => mon.name === `${sideIndex ? 'foe' : 'own'}${action.switch}`);
    return wanted ? `switch ${side.pokemon.indexOf(wanted) + 1}` : '';
  }
  const requested = request.active?.[0]?.moves || [];
  const index = requested.findIndex(move => toID(move.id || move.move) === action.move && !move.disabled);
  if (index < 0) return '';
  return `move ${index + 1}${action.tera ? ' terastallize' : ''}`;
}

// How readily the opponent switches, calibrated per opponent from scouting
// and this battle. Lower-rated players switch far less than a minimax foe.
let FOE_SWITCH_PRIOR = .45;

function options(battle, sideIndex, limit = 5, includeTera = true) {
  const side = battle.sides[sideIndex], request = side.activeRequest;
  if (!request || request.wait) return [{wait: true, prior: 1, heuristic: 0}];
  const active = side.active[0], target = side.foe.active[0];
  const result = [];
  const force = request.forceSwitch?.some(Boolean);
  const attacking = strongest(battle, target, active);
  if (!force) for (const slot of request.active?.[0]?.moves || []) {
    if (slot.disabled) continue;
    const id = toID(slot.id || slot.move), move = battle.dex.moves.get(id);
    let estimate = damage(battle, active, target, id);
    if (['fakeout', 'firstimpression'].includes(id) && active.activeMoveActions >= 1) estimate = 0;
    const priority = priorityOf(battle, active, target, move);
    let value = 70 * Math.min(1.3, estimate / Math.max(1, target.hp));
    let speedFirst = active.getActionSpeed() >= target.getActionSpeed();
    if (battle.field.pseudoWeather.trickroom) speedFirst = !speedFirst;
    const actsFirst = priority > attacking.priority || priority === attacking.priority && speedFirst;
    const priorityKO = estimate >= target.hp && priority > attacking.priority;
    // Preserve dynamic-priority finishers in the beam. Triage Draining Kiss,
    // Gale Wings and terrain-dependent Grassy Glide use official ModifyPriority;
    // their base priority alone would prune the only surviving continuation.
    if (estimate >= target.hp && actsFirst) value += attacking.damage >= active.hp ? 70 : 20;
    if (move.category === 'Status') {
      const self = move.target === 'self';
      if (move.heal) value = 35 * Math.min(1, (active.maxhp - active.hp) / active.maxhp * 2);
      else if (move.boosts && self) {
        const positive = Object.entries(move.boosts).filter(([stat, amount]) => amount > 0 && (active.boosts[stat] || 0) < 6);
        value = positive.length ? (attacking.damage < active.hp * .55 ? 22 : 2) - Math.max(active.boosts.atk, active.boosts.spa) * 3 : -30;
      }
      else if (move.status && !target.status) value = 18;
      else if (move.sideCondition && !side.foe.sideConditions[move.sideCondition]) value = side.foe.pokemonLeft > 2 ? 16 : 2;
      else if (id === 'taunt') value = 12;
      else if (id === 'protect') value = active.status === 'tox' || target.status === 'tox' ? 8 : 2;
      else if (move.forceSwitch) value = Object.values(target.boosts).some(stage => stage > 0) ? 34 : 8;
      else if (id === 'haze' || id === 'clearsmog') value = Object.values(target.boosts).some(stage => stage > 0) ? 34 : -5;
      else if (move.sideCondition && side.foe.sideConditions[move.sideCondition]) value = -30;
      else value = 6;
    }
    const prior = active.set.searchPriors?.[id] ?? 1;
    const counter = !!move.forceSwitch || ['haze', 'clearsmog', 'taunt', 'encore'].includes(id);
    result.push({move: id, tera: false, prior, heuristic: value, priorityKO, counter,
      revealed: (active.set.searchRevealedMoves || []).includes(id)});
    // Defensive Tera is explicitly simulated when the observed/inferred type
    // changes the active matchup. Unknown Tera types are scenario hypotheses.
    if (includeTera && request.active?.[0]?.canTerastallize) {
      result.push({move: id, tera: true, prior: .35 * prior, heuristic: value - 2, priorityKO, counter,
        revealed: (active.set.searchRevealedMoves || []).includes(id)});
    }
  }
  if (force || !request.active?.[0]?.trapped) {
    for (const mon of side.pokemon) {
      if (!alive(mon) || mon.isActive) continue;
      const dealt = strongest(battle, mon, target).damage / Math.max(1, target.hp);
      const received = strongest(battle, target, mon).damage / Math.max(1, mon.hp);
      let value = 22 * cap(dealt, 0, 1.5) - 32 * cap(received, 0, 1.5);
      if (!force) {
        const currentThreat = attacking.damage / Math.max(1, active.hp);
        value += currentThreat >= .95 ? 30 : -8;
      }
      result.push({switch: 0, name: mon.name, prior: force ? 1 : sideIndex ? FOE_SWITCH_PRIOR : .55, heuristic: value});
    }
  }
  result.sort((a, b) => b.heuristic - a.heuristic);
  // Preserve a counter-option: e.g. a nonattacking response to Sucker Punch,
  // or an immunity switch even when its shallow heuristic ranks lower.
  const selected = result.slice(0, limit);
  const switchOption = result.find(option => option.switch !== undefined);
  const statusOption = result.find(option => option.move && battle.dex.moves.get(option.move).category === 'Status');
  const priorityOption = result.find(option => option.priorityKO);
  for (const extra of [priorityOption, switchOption, statusOption]) {
    if (extra && !selected.includes(extra) && selected.length < limit + 2) selected.push(extra);
  }
  // Four revealed moves are actual choices, not priors. Never prune a known
  // lethal attack or a known phazing/denial response because Tera duplicates,
  // Roost or shallow utility ordering filled the beam.
  for (const extra of result.filter(option => option.move && !option.tera && (option.revealed || option.counter))) {
    if (!selected.includes(extra)) selected.push(extra);
  }
  return selected.length ? selected : [{wait: true, prior: 1, heuristic: 0}];
}

function settle(battle, stats, budget, remaining = 5) {
  // Resolve U-turn, Baton Pass, forced faint replacements and hazards before
  // evaluating the leaf. Preserve both sides' available replacements.
  if (battle.ended || battle.requestState !== 'switch' || remaining <= 0) return battle;
  const own = options(battle, 0, 3, false), foes = options(battle, 1, 3, false);
  let best = -Infinity, chosen;
  for (const action of own) {
    let worst = Infinity, worstBattle;
    for (const response of foes) {
      // A pivot/faint request is part of the current turn. Complete it even
      // at the soft search deadline; partially resolving U-turn changes whose
      // Pokémon receives the queued attack and corrupts the root comparison.
      if (stats.nodes > 10000) break;
      const branch = clone(battle.toJSON());
      const a = actionChoice(branch, 0, action), b = actionChoice(branch, 1, response);
      if (!a && !branch.sides[0].activeRequest?.wait || !b && !branch.sides[1].activeRequest?.wait) continue;
      try { branch.makeChoices(a, b); stats.nodes++; } catch (_) { continue; }
      const leaf = settle(branch, stats, budget, remaining - 1);
      const value = evaluate(leaf);
      if (value < worst) { worst = value; worstBattle = leaf; }
    }
    if (worst > best && worstBattle) { best = worst; chosen = worstBattle; }
  }
  return chosen || battle;
}

function simulate(snapshot, own, foe, stats, budget, sample = 0) {
  const branch = clone(snapshot);
  // Common random numbers compare alternatives under the same rolls rather
  // than awarding a different lucky seed to whichever action is visited first.
  const pair = Math.floor(sample / 2);
  branch.prng = new PRNG([71 + pair * 113, 97, 193 + pair * 61, branch.turn + 17]);
  if (sample % 2) {
    // Antithetic rolls pair u with 1-u: a 50% secondary effect cannot
    // accidentally occur in both root samples and appear guaranteed. The
    // official engine still applies accuracy, criticals, damage and effects.
    const next = branch.prng.rng.next.bind(branch.prng.rng);
    branch.prng.rng.next = () => 0xFFFFFFFF - next();
  }
  // Stratify the two equally likely orders of a real speed tie. With only two
  // arbitrary seeds, both samples could otherwise choose the same winner.
  // Priority, speed, Trick Room and fractional priority still come from the
  // official comparator; only an exact tie between opposing moves is split.
  const speedSort = branch.speedSort;
  branch.speedSort = function (list, comparator = this.comparePriority) {
    return speedSort.call(this, list, (a, b) => {
      const compared = comparator.call(this, a, b);
      if (compared || a.choice !== 'move' || b.choice !== 'move' ||
          !a.pokemon || !b.pokemon || a.pokemon.side.n === b.pokemon.side.n) return compared;
      return (sample % 2 ? -1 : 1) * (a.pokemon.side.n - b.pokemon.side.n);
    });
  };
  const a = actionChoice(branch, 0, own), b = actionChoice(branch, 1, foe);
  if (!a && !branch.sides[0].activeRequest?.wait || !b && !branch.sides[1].activeRequest?.wait) return null;
  try { branch.makeChoices(a, b); stats.nodes++; } catch (_) { return null; }
  return settle(branch, stats, budget);
}

function directDamage(before, after) {
  // Read the official resolved protocol, rather than treating every HP change
  // as damage from the chosen attack. Life Orb, contact recoil, hazards and
  // Disguise costs have explicit [from] annotations and remain position costs.
  const hp = new Map(before.getAllPokemon().map(mon => [mon.name, mon.hp / mon.maxhp]));
  const dealt = new Map();
  let privateNext = false, skipPublic = false;
  for (const line of after.log.slice(before.log.length)) {
    if (line.startsWith('|split|')) { privateNext = true; continue; }
    if (skipPublic) { skipPublic = false; continue; }
    if (privateNext) { privateNext = false; skipPublic = true; }
    const parts = line.split('|');
    const event = parts[1];
    if (!['-damage', '-heal', 'switch', 'drag'].includes(event)) continue;
    const name = parts[2]?.split(': ').slice(1).join(': ');
    const condition = parts[event === 'switch' || event === 'drag' ? 4 : 3] || '';
    const match = /^(\d+)(?:\/(\d+))?/.exec(condition);
    if (!name || !match) continue;
    const current = Number(match[1]) / Number(match[2] || 100);
    const previous = hp.get(name);
    if (event === '-damage' && previous !== undefined && !parts.slice(4).some(value => value.startsWith('[from]'))) {
      dealt.set(name, (dealt.get(name) || 0) + Math.max(0, previous - current));
    }
    hp.set(name, current);
  }
  return dealt;
}

// Opponent model. Real players (especially below 1600) pick strong replies
// often, but not omnisciently: a pure worst-case reply made every coverage
// move look certain, so staying in always looked worse than retreating and the
// bot switched endlessly. Replies are weighted by a softmin over their value
// for us (better for the opponent = more likely), scaled by the move prior.
let REPLY_TEMPERATURE = 28;
let SAFETY_BASE = .08;
// What this foe has actually kept choosing in this battle ({moves, weight}).
// The softmin assumes the reply that is best for the opponent; a player who
// keeps answering Sucker Punch with setup is not playing that reply.
let FOE_HABIT = null;

function replyWeights(groups) {
  const worst = Math.min(...groups.map(group => group.mean));
  const raw = groups.map(group => group.prior * Math.exp(-(group.mean - worst) / REPLY_TEMPERATURE));
  const total = raw.reduce((sum, value) => sum + value, 0) || 1;
  const soft = raw.map(value => value / total);
  if (!FOE_HABIT) return soft;
  const habit = groups.map(group => FOE_HABIT.moves[String(group.response ?? '').split(':')[0]] || 0);
  const mass = habit.reduce((sum, value) => sum + value, 0);
  if (!mass) return soft;
  return soft.map((value, i) => (1 - FOE_HABIT.weight) * value + FOE_HABIT.weight * habit[i] / mass);
}

function groupRows(rows) {
  const hypotheses = new Map();
  rows.forEach((row, index) => {
    const hypothesis = row.hypothesis ?? 0, response = row.response ?? String(index);
    if (!hypotheses.has(hypothesis)) hypotheses.set(hypothesis, new Map());
    const replies = hypotheses.get(hypothesis);
    if (!replies.has(response)) replies.set(response, []);
    replies.get(response).push(row);
  });
  return [...hypotheses.values()].map(replies => [...replies.entries()].map(([response, samples]) => ({samples, response,
    // Damage rolls/misses belong to chance, not adversarial choice. An
    // opponent can select Thunderbolt, but cannot select its critical roll.
    mean: samples.reduce((sum, row) => sum + row.value, 0) / samples.length,
    prior: Math.max(.0001, samples[0].weight || 1)})));
}

function aggregate(rows, risk) {
  if (!rows.length) return {score: -1e6, worst: -1e6};
  const scenarios = groupRows(rows).map(groups => {
    const weights = replyWeights(groups);
    const worst = Math.min(...groups.map(group => group.mean));
    return {value: groups.reduce((sum, group, i) => sum + group.mean * weights[i], 0), worst};
  });
  const expected = scenarios.reduce((sum, scenario) => sum + scenario.value, 0) / scenarios.length;
  const worstReply = Math.min(...scenarios.map(scenario => scenario.worst));
  // A bounded safety term keeps clearly losing lines (a known lethal reply)
  // unattractive without letting every speculative reply dominate the score.
  const safety = SAFETY_BASE + .17 * cap(risk, 0, 1);
  const weighted = probabilitySamples(rows, risk);
  const mean = weighted.reduce((sum, row) => sum + row.probability * row.value, 0);
  const deviation = Math.sqrt(weighted.reduce((sum, row) => sum + row.probability * (row.value - mean) ** 2, 0));
  return {score: (1 - safety) * expected + safety * worstReply, mean, deviation, worst: worstReply};
}

function probabilitySamples(rows, risk) {
  // Equal mass per hidden-set hypothesis; replies follow the opponent model,
  // while random rolls within a decision remain equally likely.
  const hypotheses = groupRows(rows);
  const output = [];
  for (const groups of hypotheses) {
    const weights = replyWeights(groups);
    groups.forEach((group, i) => {
      const probability = weights[i] / hypotheses.length / group.samples.length;
      for (const row of group.samples) output.push({...row, probability});
    });
  }
  return output;
}

function responseKey(action) {
  return action.name || (action.switch !== undefined ? `switch${action.switch}` : `${action.move || 'wait'}${action.tera ? ':tera' : ''}`);
}

function nextTurnPolicy(branch, stats, budget, risk, sample, breadth = 2) {
  if (branch.ended) return {value: evaluate(branch), branch};
  // Second-turn lookahead width: wider when the clock allows (deeper thinking).
  const own = options(branch, 0, breadth, true), foes = options(branch, 1, breadth, true);
  const snapshot = branch.toJSON();
  let best = -Infinity, representative = branch;
  for (const action of own) {
    const replies = [];
    for (const response of foes) {
      const child = simulate(snapshot, action, response, stats, budget, sample);
      if (child) replies.push({value: evaluate(child), weight: response.prior || 1, response: responseKey(response), branch: child});
    }
    if (replies.length) {
      const value = aggregate(replies, risk).score;
      if (value > best) {
        best = value;
        representative = [...replies].sort((a, b) => a.value - b.value)[0].branch;
      }
    }
  }
  return {value: Number.isFinite(best) ? best : evaluate(branch), branch: representative};
}

function nextTurnValue(branch, stats, budget, risk, sample, breadth = 2) {
  return nextTurnPolicy(branch, stats, budget, risk, sample, breadth).value;
}

function conditionedHypotheses(payload, warnings) {
  const evidence = payload.feedback?.observed_damage || [];
  const choices = payload.feedback?.observed_choices || [];
  if (!evidence.length && !choices.length) return {order: payload.opponents.map((_, i) => i).slice(0, payload.hypothesisLimit || 3), errors: [], fitted: 0, choices: 0};
  const observedState = (data, mon, hp) => ({...data, hp,
    actualHp: data.actualMaxHp ? Math.round(data.actualMaxHp * hp) : null,
    boosts: mon.boosts || data.boosts, status: toID(mon.status || ''),
    stats: Object.fromEntries(Object.entries(mon.stats || {}).filter(([, value]) => Number.isFinite(value))),
    types: (mon.types || data.types).map(value => value[0] + value.slice(1).toLowerCase()),
    effects: Object.fromEntries(Object.entries(mon.effects || {}).map(([key, value]) => [toID(key), value])),
    terastallized: mon.terastallized ? mon.tera_type[0] + mon.tera_type.slice(1).toLowerCase() : '',
    set: {...data.set, species: mon.species,
      item: mon.item === 'unknown_item' ? data.set.item : toID(mon.item || ''),
      ability: mon.ability ? toID(mon.ability) : data.set.ability}});
  const fits = payload.opponents.map((_, hypothesis) => {
    const battle = reconstruct(payload, hypothesis, 0, warnings);
    let loss = 0, count = 0;
    for (const observation of evidence) {
      const side = observation.attacker === 'own' ? 0 : 1;
      const attacker = battle.sides[side].active[0], defender = battle.sides[1 - side].active[0];
      const move = battle.dex.moves.get(observation.move);
      if (!move.exists || move.category === 'Status' || !alive(attacker) || !alive(defender)) continue;
      // HP-dependent moves are evaluated at the observed attack's HP, rather
      // than copying its damage into a later, weaker Eruption/Water Spout.
      const saved = attacker.hp;
      if (Number.isFinite(observation.attacker_hp)) attacker.hp = Math.max(1, Math.round(attacker.maxhp * observation.attacker_hp));
      const predicted = damage(battle, attacker, defender, move.id) / defender.maxhp;
      attacker.hp = saved;
      const tolerance = .025 + observation.amount * .16; // roll and public HP rounding
      const error = observation.censored ? Math.max(0, observation.amount - predicted) : Math.abs(observation.amount - predicted);
      loss += Math.min(12, Math.max(0, error - tolerance) / Math.max(.02, tolerance));
      count++;
    }
    let choiceLoss = 0, choicesUsed = 0;
    for (const observation of choices) {
      const move = battle.dex.moves.get(observation.move);
      // Status moves can be purposeful support, and prediction can favor
      // utility. Only repeatedly declining an available lethal attack is
      // weak evidence against a hidden coverage build, never an impossibility.
      if (!move.exists || move.category === 'Status') continue;
      const target = payload.own.find(data => baseSpeciesID(data.set.species) === baseSpeciesID(observation.target.species));
      if (!target || !observation.attacker || observation.target_hp <= 0 || observation.attacker_hp <= 0) continue;
      // A past 1v1 snapshot that cannot be rebuilt is weak evidence, not a
      // reason to lose the whole search (ladder: 30 emergency fallbacks).
      let historical;
      try {
        historical = reconstruct({...payload, turn: observation.turn,
        own: [observedState(target, observation.target, observation.target_hp)],
        opponents: [[observedState(payload.opponents[hypothesis][0], observation.attacker, observation.attacker_hp)]],
        actions: [], available: [], forceSwitch: false, canTera: false, opponentCanTera: false,
        sideConditions: observation.conditions.map(row => Object.fromEntries(Object.entries(row).map(([key, value]) => [toID(key), value]))),
        weather: Object.fromEntries(observation.weather.map(key => [toID(key), observation.turn])),
        fields: Object.fromEntries(observation.fields.map(key => [toID(key), observation.turn]))}, 0, 0, warnings);
      } catch (_) { continue; }
      const attacker = historical.sides[1].active[0], defender = historical.sides[0].active[0];
      const best = strongest(historical, attacker, defender);
      const selected = damage(historical, attacker, defender, observation.move);
      if (best.damage >= defender.hp * 1.1 && selected < defender.hp * .45 && selected < best.damage * .4) {
        choiceLoss += .3;
      }
      choicesUsed++;
    }
    return {hypothesis, loss: (count ? loss / count : 0) + Math.min(.9, choiceLoss), count, choicesUsed};
  }).sort((a, b) => a.loss - b.loss || a.hypothesis - b.hypothesis);
  const best = fits[0].loss;
  const plausible = fits.filter(fit => fit.loss <= best + .8).slice(0, payload.hypothesisLimit || 3);
  if (best > 1) warnings.add('No hypothesis explains the observed damage well; the set inference stays uncertain.');
  return {order: plausible.map(fit => fit.hypothesis), errors: fits.map(({hypothesis, loss}) => ({hypothesis, error: +loss.toFixed(3)})),
    fitted: evidence.length, choices: Math.max(...fits.map(fit => fit.choicesUsed))};
}

function configureOpponent(payload) {
  FOE_SWITCH_PRIOR = cap(Number(payload.foeSwitchPrior ?? .45), .1, 1);
  // Opponent realism: higher temperature = a less clairvoyant opponent.
  REPLY_TEMPERATURE = cap(Number(payload.replyTemperature ?? 28), 5, 200);
  SAFETY_BASE = cap(Number(payload.safetyBase ?? .08), 0, .5);
  BRAIN_V2 = payload.brain === 'v2';
  const habit = payload.foeHabit;
  FOE_HABIT = habit && habit.moves && Object.keys(habit.moves).length
    ? {moves: habit.moves, weight: cap(Number(habit.weight) || 0, 0, .8)} : null;
}

function search(payload) {
  const start = Date.now();
  configureOpponent(payload);
  const budget = {deadline: Math.min(payload.deadline || Infinity, start + cap(payload.timeBudgetMs || 1500, 200, 15000))};
  const stats = {nodes: 0}, warnings = new Set();
  const fitted = payload.fitted || conditionedHypotheses(payload, warnings);
  if (payload.hypothesisIndices) fitted.order = payload.hypothesisIndices;
  const reconstructed = reconstruct(payload, fitted.order[0], 0, warnings);
  const baseValue = evaluate(reconstructed);
  const roots = payload.actions.map(action => ({action, rows: [], shallow: [], samples: [], depth: 1, legal: true}));
  const risk = cap(payload.risk ?? .35, 0, 1);
  const breadth = cap(payload.breadth || 2, 2, 4);
  const hypotheses = fitted.order.length;
  let completedScenarios = 0;
  let completedDepth = 1;
  // Each complete round covers EVERY root. Do not stop halfway and compare a
  // thoroughly searched move with one evaluated only under a favorable sample.
  for (let scenario = 0; scenario < hypotheses; scenario++) {
    if (scenario && Date.now() >= budget.deadline - 100) break;
    const hypothesis = fitted.order[scenario];
    const state = scenario ? reconstruct(payload, hypothesis, 0, warnings) : reconstructed;
    const snapshot = state.toJSON();
    const foes = options(state, 1, payload.allResponses ? 32 : 3, true);
    const round = [];
    for (const root of roots) {
      const rows = [], evidence = [];
      for (const foe of foes) {
        for (let roll = 0; roll < 2; roll++) {
          const sample = hypothesis * (payload.allResponses ? 64 : 2) + (payload.rollOffset || 0) + roll;
          const branch = simulate(snapshot, root.action, foe, stats, budget, sample);
          if (!branch) continue;
          let value = evaluate(branch) - baseValue;
          let scoringBranch = branch;
          if (payload.forceSwitch && !branch.ended) {
            // A replacement request consumes no battle turn. The mandatory
            // first pass includes its first actual attacking turn even when
            // the optional second-turn budget is exhausted. This prevents a
            // slow replacement from looking safe before being immediately KO'd.
            const attack = nextTurnPolicy(branch, stats, budget, risk, sample + 5, breadth);
            value = attack.value - baseValue;
            scoringBranch = attack.branch;
          }
          const ownTarget = root.action.switch !== undefined ? `own${root.action.switch}` : 'own0';
          const evidenceBefore = payload.forceSwitch ? branch : state;
          const evidenceAfter = payload.forceSwitch ? scoringBranch : branch;
          const foeTarget = payload.forceSwitch ? branch.sides[1].active[0]?.name : 'foe0';
          const attackDamage = directDamage(evidenceBefore, evidenceAfter);
          const hazard = ['stealthrock', 'spikes', 'toxicspikes', 'stickyweb'].includes(root.action.move) ? root.action.move : null;
          const hazardLayers = side => side.sideConditions[hazard]?.layers || (side.sideConditions[hazard] ? 1 : 0);
          evidence.push({value, weight: foe.prior || 1, response: responseKey(foe), hypothesis,
            terminalWin: branch.ended && branch.winner === 'Bot' ? 1 : 0,
            terminalLoss: branch.ended && branch.winner === 'Opponent' ? 1 : 0,
            received: attackDamage.get(ownTarget) || 0,
            dealt: attackDamage.get(foeTarget) || 0,
            hazardRetained: hazard && hazardLayers(branch.sides[1]) > hazardLayers(state.sides[1]) ? 1 : 0,
            faintRisk: !alive(evidenceAfter.sides[0].pokemon.find(mon => mon.name === ownTarget)) ? 1 : 0,
            koChance: alive(evidenceBefore.sides[1].pokemon.find(mon => mon.name === foeTarget)) &&
              !alive(evidenceAfter.sides[1].pokemon.find(mon => mon.name === foeTarget)) ? 1 : 0});
          rows.push({value, weight: foe.prior || 1, branch: scoringBranch, sample,
            response: responseKey(foe), hypothesis});
        }
      }
      round.push({root, rows, evidence});
    }
    // Finish depth two in the first hypothesis before spending the budget on
    // more hidden-set samples. Previously, three shallow ensembles exhausted
    // the budget in pivot-heavy 6v6 games and never reached tactical depth.
    const deep = [];
    let timedOut = false;
    if (payload.depth >= 2 && Date.now() < budget.deadline - 60) {
      for (const {root, rows} of round) {
        const grouped = new Map();
        for (const row of rows) {
          if (!grouped.has(row.response)) grouped.set(row.response, []);
          grouped.get(row.response).push(row);
        }
        const replies = [...grouped.values()].sort((a, b) => (
          a.reduce((sum, row) => sum + row.value, 0) / a.length -
          b.reduce((sum, row) => sum + row.value, 0) / b.length));
        const selected = replies.slice(0, breadth).flat();
        const deepRows = [];
        for (const row of selected) {
          if (Date.now() >= budget.deadline) { timedOut = true; break; }
          const later = nextTurnValue(row.branch, stats, budget, risk, row.sample + 7, breadth) - baseValue;
          // A resolved terminal result is absorbing. Mixing a first-turn
          // survival score into certain next-turn defeat rewarded waiting for
          // the same loss instead of making the remaining productive trade.
          deepRows.push({value: Math.abs(later + baseValue) >= 9000 ? later : .15 * row.value + .85 * later, weight: row.weight,
            response: row.response, hypothesis: row.hypothesis});
        }
        if (timedOut) break;
        deep.push({root, rows: deepRows});
      }
    }
    const fullDeep = !timedOut && deep.length === roots.length;
    // Once candidates have depth two, never contaminate their comparison with
    // a partial or shallow subsequent ensemble. A full round is transactional.
    if (completedDepth === 2 && !fullDeep) break;
    if (fullDeep) {
      for (const {root, rows} of deep) root.rows.push(...rows);
      completedDepth = 2;
    } else {
      for (const {root, rows} of round) root.rows.push(...rows);
    }
    for (const {root, evidence, rows} of round) {
      root.samples.push(...evidence);
      root.shallow.push(...rows);
    }
    completedScenarios++;
    if (timedOut) break;
  }
  const result = summarize(roots, risk, completedDepth);
  if (payload.returnEvidence) result.evidence = roots.map(root => ({key: root.action.key,
    rows: root.rows.map(({branch, ...row}) => row),
    shallow: root.shallow.map(({branch, ...row}) => row), samples: root.samples}));
  return {...result, diagnostics: {engine: 'official-pokemon-showdown', nodes: stats.nodes,
    depth: completedDepth, scenarios: completedScenarios, workers: 1, elapsed_ms: Date.now() - start,
    conditioned_hypotheses: fitted.order, observed_damage_samples: fitted.fitted, hypothesis_errors: fitted.errors,
    observed_choice_samples: fitted.choices,
    forced_loss_trade: result.forcedLoss, strategy: payload.strategy?.archetype || 'unspecified',
    time_budget_ms: payload.timeBudgetMs, warnings: [...warnings],
    uncertainty: 'Hidden items, abilities, unrevealed moves, Tera and EVs are sampled hypotheses; nature+EV spread is preserved jointly.'}};
}

// ---- Brain v2: simultaneous choice ------------------------------------------
// Both players choose at once. The legacy aggregate scored each of our actions
// against replies picked *knowing* that action, so every attack looked
// punished by the perfect counter-switch and the bot retreated endlessly (the
// sequential-model flaw Foul Play's author dropped expectiminimax for).
// Here one opponent policy is computed for the whole matrix: a quantal
// response equilibrium between both sides, then our actions are compared
// against that same policy, with a bounded worst-case safety term.
function simultaneousScores(roots, risk) {
  const cells = new Map(); // hypothesis -> rootIndex -> response -> {sum, n, weight}
  const add = (rows, rootIndex, deep) => {
    for (const row of rows) {
      const h = row.hypothesis ?? 0, r = row.response ?? 'wait';
      if (!cells.has(h)) cells.set(h, new Map());
      const byRoot = cells.get(h);
      if (!byRoot.has(rootIndex)) byRoot.set(rootIndex, new Map());
      const byResponse = byRoot.get(rootIndex);
      const cell = byResponse.get(r) || {shallow: [], deep: [], weight: row.weight || 1};
      (deep ? cell.deep : cell.shallow).push(row.value);
      byResponse.set(r, cell);
    }
  };
  roots.forEach((root, i) => {
    add(root.shallow.length ? root.shallow : root.rows, i, false);
    if (root.shallow.length && root.rows !== root.shallow) add(root.rows, i, true);
  });
  const totals = roots.map(() => ({value: 0, worst: 0, n: 0}));
  const safety = SAFETY_BASE + .1 * cap(risk, 0, 1);
  for (const byRoot of cells.values()) {
    const responses = [...new Set([...byRoot.values()].flatMap(map => [...map.keys()]))];
    const indices = roots.map((_, i) => i).filter(i => byRoot.has(i));
    if (!indices.length || !responses.length) continue;
    const value = (i, r) => {
      const cell = byRoot.get(i)?.get(r);
      if (!cell) return null;
      const list = cell.deep.length ? cell.deep : cell.shallow;
      return list.reduce((sum, v) => sum + v, 0) / list.length;
    };
    const matrix = indices.map(i => responses.map(r => value(i, r)));
    const prior = responses.map(r => {
      for (const i of indices) { const cell = byRoot.get(i).get(r); if (cell) return Math.max(.0001, cell.weight); }
      return 1;
    });
    const habit = FOE_HABIT ? responses.map(r => FOE_HABIT.moves[String(r).split(':')[0]] || 0) : null;
    const habitMass = habit ? habit.reduce((sum, v) => sum + v, 0) : 0;
    let ours = indices.map(() => 1 / indices.length), theirs = responses.map(() => 1 / responses.length);
    const softmax = (values, temperature, weights) => {
      const top = Math.max(...values);
      const raw = values.map((v, k) => (weights ? weights[k] : 1) * Math.exp((v - top) / temperature));
      const total = raw.reduce((sum, v) => sum + v, 0) || 1;
      return raw.map(v => v / total);
    };
    for (let iteration = 0; iteration < 60; iteration++) {
      // Opponent: expected value of each reply against our current mix.
      const foeValue = responses.map((_, k) => {
        let sum = 0, mass = 0;
        matrix.forEach((row, j) => { if (row[k] !== null) { sum -= ours[j] * row[k]; mass += ours[j]; } });
        return mass ? sum / mass : -Infinity;
      });
      let next = softmax(foeValue.map(v => Number.isFinite(v) ? v : -1e6), REPLY_TEMPERATURE, prior);
      if (habit && habitMass) next = next.map((v, k) => (1 - FOE_HABIT.weight) * v + FOE_HABIT.weight * habit[k] / habitMass);
      theirs = theirs.map((v, k) => .5 * v + .5 * next[k]);
      const ownValue = matrix.map(row => {
        let sum = 0, mass = 0;
        row.forEach((v, k) => { if (v !== null) { sum += theirs[k] * v; mass += theirs[k]; } });
        return mass ? sum / mass : -1e6;
      });
      const mine = softmax(ownValue, 15);
      ours = ours.map((v, j) => .5 * v + .5 * mine[j]);
    }
    indices.forEach((i, j) => {
      let sum = 0, mass = 0, worst = Infinity;
      matrix[j].forEach((v, k) => {
        if (v === null) return;
        sum += theirs[k] * v; mass += theirs[k];
        // Worst case only among replies the opponent plausibly makes.
        if (theirs[k] >= .02) worst = Math.min(worst, v);
      });
      const expected = mass ? sum / mass : -1e6;
      if (!Number.isFinite(worst)) worst = expected;
      totals[i].value += expected; totals[i].worst += worst; totals[i].n++;
    });
  }
  return totals.map(t => t.n ? {mean: t.value / t.n, worst: t.worst / t.n,
    score: (1 - safety) * t.value / t.n + safety * t.worst / t.n} : null);
}

function summarize(roots, risk, completedDepth) {
  const simultaneous = BRAIN_V2 ? simultaneousScores(roots, risk) : null;
  roots.forEach((root, index) => {
    root.legal = root.rows.length > 0;
    root.depth = completedDepth;
    Object.assign(root, aggregate(root.rows, risk));
    if (simultaneous && simultaneous[index]) Object.assign(root, simultaneous[index]);
    // Equal resolved positions still distinguish an effective last attack
    // from an immune one. This tiny tie-break cannot reverse tactical scores.
    root.score += .001 * root.samples.reduce((sum, row) => sum + row.dealt, 0) / Math.max(1, root.samples.length);
  });
  const forcedLoss = roots.length > 1 && roots.every(root => root.legal && root.worst < -9000) &&
    Math.max(...roots.map(root => root.score)) < -8500;
  if (forcedLoss) {
    // Every considered rational reply leads to defeat inside the horizon.
    // Harmless speculative replies must not favor an immune attack merely
    // because it postpones the same loss. Prefer the remaining useful trade;
    // this rule is inactive whenever a materially surviving line exists.
    for (const root of roots) {
      const average = key => root.samples.reduce((sum, row) => sum + row[key], 0) / Math.max(1, root.samples.length);
      root.originalScore = root.score;
      root.score = root.worst + 100 * average('koChance') + 50 * average('dealt');
    }
  }
  const results = roots.map(root => {
    const evidence = probabilitySamples(root.samples, risk);
    const mass = evidence.reduce((sum, sample) => sum + sample.probability, 0) || 1;
    const average = key => evidence.reduce((sum, sample) => sum + sample[key] * sample.probability, 0) / mass;
    const deviation = key => Math.sqrt(evidence.reduce((sum, sample) => sum + (sample[key] - average(key)) ** 2 * sample.probability, 0) / mass);
    const groups = new Map();
    for (const sample of root.samples) {
      const key = `${sample.hypothesis}:${sample.response}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(sample);
    }
    // Adversarial threats are means over chance, never a cherry-picked crit.
    // Keep them separate from the response-weighted positional expectation.
    const threat = key => Math.max(0, ...[...groups.values()].map(samples =>
      samples.reduce((sum, row) => sum + row[key], 0) / samples.length));
    return {key: root.action.key, legal: root.legal, score: root.score, worst: root.worst,
      mean: root.mean, deviation: root.deviation, damageStd: deviation('dealt'), receivedStd: deviation('received'),
      winProbability: average('terminalWin'), lossProbability: average('terminalLoss'),
      depth: root.depth, samples: root.rows.length, received: average('received'), dealt: average('dealt'),
      receivedWorst: threat('received'), faintRiskWorst: threat('faintRisk'), responses: groups.size,
      hazardRetention: ['stealthrock', 'spikes', 'toxicspikes', 'stickyweb'].includes(root.action.move) ? average('hazardRetained') : null,
      faintRisk: average('faintRisk'), koChance: average('koChance'), terminalTrade: forcedLoss,
      originalScore: root.originalScore};
  });
  return {results, forcedLoss};
}

function inspect(payload) {
  const warnings = new Set();
  let battle = reconstruct(payload, payload.hypothesis || 0, 0, warnings);
  if (payload.testActions) {
    const stats = {nodes: 0}, budget = {deadline: Date.now() + 10000};
    battle = simulate(battle.toJSON(), payload.testActions[0], payload.testActions[1], stats, budget, payload.sample || 0);
    if (!battle) throw new Error('Illegal test action');
  }
  return {sides: battle.sides.map(side => ({pokemonLeft: side.pokemonLeft,
    request: side.activeRequest, conditions: Object.keys(side.sideConditions),
    pokemon: side.pokemon.map(mon => ({name: mon.name, species: mon.species.id, hp: mon.hp, maxhp: mon.maxhp,
      status: mon.status, boosts: mon.boosts, stats: mon.storedStats, item: mon.item, ability: mon.ability,
      moves: mon.moveSlots, effects: Object.keys(mon.volatiles), tera: mon.terastallized || '', active: mon.isActive}))})),
    weather: battle.field.weather, terrain: battle.field.terrain, fields: Object.keys(battle.field.pseudoWeather),
    ended: battle.ended, winner: battle.winner, log: battle.log, warnings: [...warnings],
    pressure: battle.sides.map(side => Object.fromEntries(side.active[0].moveSlots.map(slot =>
      [slot.id, damage(battle, side.active[0], side.foe.active[0], slot.id)]))),
    evaluation: evaluate(battle), options: battle.sides.map((_, i) => options(battle, i, 3, true).map(action => ({
      move: action.move, switch: action.switch, tera: !!action.tera, name: action.name, heuristic: action.heuristic})))};
}

function preview(payload) {
  const started = Date.now(), budget = {deadline: started + (payload.timeBudgetMs || 3000)};
  const stats = {nodes: 0}, warnings = new Set();
  FOE_HABIT = null; // workers are shared between battles: no habit at preview
  BRAIN_V2 = payload.brain === 'v2';
  const team = payload.own, foes = payload.opponents[0];
  if (!team.length || !foes.length) throw new Error('Preview sem times conhecidos.');
  const scores = team.map(() => []);
  let completed = 0;
  for (let foeIndex = 0; foeIndex < foes.length; foeIndex++) {
    if (completed && Date.now() >= budget.deadline - 100) break;
    const round = [];
    for (let ownIndex = 0; ownIndex < team.length; ownIndex++) {
      const ownTeam = [team[ownIndex], ...team.filter((_, i) => i !== ownIndex)];
      const opposingTeam = [foes[foeIndex], ...foes.filter((_, i) => i !== foeIndex)];
      const state = reconstruct({...payload, preview: true, own: ownTeam, opponents: [opposingTeam]}, 0, 0, warnings);
      const snapshot = state.toJSON();
      const actions = options(state, 0, 3, false), responses = options(state, 1, 3, true);
      let best = -Infinity;
      for (const action of actions) {
        const rows = [];
        for (const response of responses) {
          const branch = simulate(snapshot, action, response, stats, budget, foeIndex + 11);
          if (branch) rows.push({value: evaluate(branch), weight: response.prior || 1, response: responseKey(response)});
        }
        if (rows.length) best = Math.max(best, aggregate(rows, payload.risk ?? .35).score);
      }
      round.push(best);
    }
    // Complete each opposing-lead round for all our possible leads, keeping a
    // fair comparison even if the preview budget prevents another round.
    for (let i = 0; i < team.length; i++) scores[i].push(round[i]);
    completed++;
  }
  // Scouted lead frequencies (this player's history) weight each foe lead;
  // a uniform floor keeps unexpected leads in the comparison.
  const leadWeights = (payload.foeLeadWeights || []).slice(0, completed);
  const scouted = leadWeights.some(value => value > 0);
  const ranking = scores.map((values, i) => {
    const weights = values.map((_, foe) => scouted ? .15 / values.length + (leadWeights[foe] || 0) : 1);
    const mass = weights.reduce((sum, value) => sum + value, 0) || 1;
    const mean = values.reduce((sum, value, foe) => sum + value * weights[foe], 0) / mass;
    const worst = Math.min(...values), risk = .45 + .35 * cap(payload.risk ?? .35, 0, 1);
    const roles = strategyFor(payload.strategy, team[i].set.species)?.roles || [];
    // The team's game plan decides who leads; the one-turn matrix only
    // breaks ties and avoids catastrophic leads. HO leads with its field setter.
    const style = payload.strategy?.archetype;
    const plan = style === 'hyper_offense' ?
      Math.min(16, (roles.includes('suicide_lead') ? 12 : 0) + (roles.includes('web_setter') ? 6 : 0) +
        (roles.includes('screens') ? 6 : 0) + (roles.includes('hazard_setter') ? 4 : 0)) :
      style === 'weather' ? (roles.includes('weather_setter') ? 8 : 0) :
      (roles.includes('hazard_setter') ? 5 : 0) + (roles.includes('pivot') ? 2 : 0);
    return {slot: i + 1, score: (1 - risk) * mean + risk * worst + plan, worst, strategy_adjustment: plan};
  }).sort((a, b) => b.score - a.score || a.slot - b.slot);
  const lead = ranking[0].slot;
  return {order: [lead, ...team.map((_, i) => i + 1).filter(slot => slot !== lead)], ranking,
    diagnostics: {engine: 'official-pokemon-showdown', mode: 'lead-preview', nodes: stats.nodes,
      foe_leads: completed, total_foe_leads: foes.length, elapsed_ms: Date.now() - started,
      warnings: [...warnings], uncertainty: 'First-turn sample against plausible leads and hidden sets; it does not guarantee the opposing lead.'}};
}

// Warm the generation/dex and common battle paths during startup, before a
// user's first timed turn. Lazy module loading should not consume its search
// budget or cause an otherwise complete tactical round to be discarded.
Dex.mod('gen9').includeData();
const warmup = new Battle({formatid: 'gen9customgame', seed: [1, 2, 3, 4],
  p1: {name: 'Warmup1', team: [{species: 'Mew', ability: 'Synchronize', moves: ['tackle']}]},
  p2: {name: 'Warmup2', team: [{species: 'Mew', ability: 'Synchronize', moves: ['tackle']}]}});
if (warmup.requestState === 'teampreview') warmup.makeChoices('team 1', 'team 1');
warmup.makeChoices('move 1', 'move 1');
function dispatch(payload) {
  if (payload.op === 'health') return {ready: true, engine: 'official-pokemon-showdown', version: '0.11.11', protocol: 1};
  if (payload.op === 'validate') {
    const team = Showdown.Teams.import(payload.team);
    return {errors: team?.length ? (new Showdown.TeamValidator(payload.format)).validateTeam(team) || [] : ['Empty team or invalid export.']};
  }
  if (payload.op === 'preview') return preview(payload);
  return payload.op === 'inspect' ? inspect(payload) : search(payload);
}

if (!isMainThread) {
  parentPort.postMessage({ready: true});
  parentPort.on('message', ({id, payload}) => {
    try { parentPort.postMessage({id, result: dispatch(payload)}); }
    catch (error) { parentPort.postMessage({id, error: String(error.message)}); }
  });
} else if (Number(process.env.SHOWDOWN_SEARCH_WORKERS) > 1) {
  require('./simulator_pool').start({dispatch, summarize, conditionedHypotheses, configureOpponent});
} else {
  process.stdout.write(JSON.stringify({ready: true, simulator: 'pokemon-showdown', protocol: 1}) + '\n');
  const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  input.on('line', line => {
    try { process.stdout.write(JSON.stringify(dispatch(JSON.parse(line))) + '\n'); }
    catch (error) { process.stdout.write(JSON.stringify({error: String(error.message), stack: error.stack}) + '\n'); }
  });
}
