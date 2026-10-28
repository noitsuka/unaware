'use strict';
// A persistent shared-process CPU pool. Terminating its Node process terminates
// every worker thread, including a simulator branch inside synchronous code.
const {Worker} = require('worker_threads');
const os = require('os');
const path = require('path');
const readline = require('readline');

class Pool {
  constructor(size) {
    this.size = size;
    this.slots = Array.from({length: size}, () => this.spawn());
    this.queue = [];
    this.sequence = 0;
  }
  spawn() {
    const slot = {worker: new Worker(path.join(__dirname, 'simulator_worker.js')), ready: false, job: null, retired: false};
    slot.readyPromise = new Promise((resolve, reject) => {
      slot.worker.on('message', message => {
        if (message.ready) { slot.ready = true; resolve(); this.pump(); return; }
        const job = slot.job;
        if (!job || job.id !== message.id) return;
        slot.job = null;
        if (message.error) job.reject(new Error(message.error));
        else {
          if (message.result.diagnostics) message.result.diagnostics.executor_thread_id = slot.worker.threadId;
          job.resolve(message.result);
        }
        this.pump();
      });
      slot.worker.on('error', error => {
        reject(error);
        if (slot.job) { slot.job.reject(error); slot.job = null; }
        slot.ready = false;
        this.replace(slot);
      });
      slot.worker.on('exit', code => {
        slot.ready = false;
        if (slot.job) { slot.job.reject(new Error(`Simulador interrompido (${code}).`)); slot.job = null; }
        this.replace(slot);
      });
    });
    // A replacement may fail while idle; the next request surfaces readiness.
    slot.readyPromise.catch(() => {});
    return slot;
  }
  replace(slot) {
    if (slot.retired || !this.slots) return;
    const index = this.slots.indexOf(slot);
    if (index < 0) return;
    slot.retired = true;
    slot.worker.terminate();
    this.slots[index] = this.spawn();
  }
  pump() {
    if (!this.slots || !this.queue) return;
    for (const slot of this.slots) {
      if (!slot.ready || slot.job || !this.queue.length) continue;
      slot.job = this.queue.shift();
      slot.worker.postMessage({id: slot.job.id, payload: slot.job.payload});
    }
  }
  run(payload) {
    return new Promise((resolve, reject) => {
      this.queue.push({id: ++this.sequence, payload, resolve, reject});
      this.pump();
    });
  }
  cancel() {
    const error = new Error('Analysis deadline reached.');
    for (const job of this.queue.splice(0)) job.reject(error);
    this.slots = this.slots.map(slot => {
      if (!slot.job) return slot;
      slot.job.reject(error);
      slot.job = null;
      slot.retired = true;
      slot.worker.terminate();
      return this.spawn();
    });
  }
}

async function parallelSearch(pool, core, payload) {
  core.configureOpponent(payload);
  const began = Date.now();
  const deadline = began + Math.max(200, Math.min(15000, payload.timeBudgetMs || 7000));
  // A heuristic fallback is far worse than a slightly slower full answer:
  // the first round may overrun by a bounded grace (the clock already keeps
  // a 2 s safety margin before the server timer).
  const grace = Math.max(0, Math.min(3000, payload.graceMs ?? (payload.timeBudgetMs || 7000) * .4));
  const warnings = new Set();
  const fitted = core.conditionedHypotheses(payload, warnings);
  const roots = payload.actions.map(action => ({action, rows: [], shallow: [], samples: []}));
  const indices = new Map(roots.map(root => [root.action.key, root]));
  let rounds = 0, deepRounds = 0, nodes = 0, discarded = 0, timedOut = false;
  const hypotheses = new Set(), workersUsed = new Set();
  // When few legal actions remain, parallelize independent hypotheses/rolls
  // as well. One or two moves can still occupy the whole available CPU pool.
  const width = Math.max(1, Math.ceil(pool.size / Math.max(1, roots.length)));
  const maxRounds = fitted.order.length * 5;
  for (let first = 0; first < maxRounds && Date.now() < deadline - 80; first += width) {
    const completed = [];
    let failure;
    const groups = [];
    for (let round = first; round < Math.min(maxRounds, first + width); round++) {
      const hypothesis = fitted.order[round % fitted.order.length];
      const rollOffset = 2 * Math.floor(round / fitted.order.length);
      const tasks = payload.actions.map(action => pool.run({...payload, actions: [action],
        fitted: {...fitted}, hypothesisIndices: [hypothesis], rollOffset,
        // The first round is always shallow so a complete comparison exists;
        // depth is added only by later rounds when time remains.
        depth: first ? payload.depth : 1, deadline: first ? deadline : deadline + grace, allResponses: true, returnEvidence: true}));
      groups.push(Promise.all(tasks).then(batch => completed.push({batch, hypothesis})).catch(error => { failure = error; }));
    }
    let timer;
    const limit = (first ? deadline : deadline + grace) - Date.now();
    const timeout = new Promise(resolve => { timer = setTimeout(() => resolve(null), Math.max(1, limit)); });
    let finished;
    try { finished = await Promise.race([Promise.all(groups), timeout]); }
    finally { clearTimeout(timer); }
    // Commit independently completed *whole matrices*, never partial roots.
    // Completion order cannot change weights or random seeds.
    for (const {batch, hypothesis} of completed) {
      const completeDeep = batch.every(result => result.diagnostics.depth === 2);
      for (const result of batch) {
        workersUsed.add(result.diagnostics.executor_thread_id);
        nodes += result.diagnostics.nodes;
        for (const warning of result.diagnostics.warnings) warnings.add(warning);
        for (const row of result.evidence) {
          const root = indices.get(row.key);
          root.shallow.push(...row.shallow);
          root.samples.push(...row.samples);
          if (completeDeep) root.rows.push(...row.rows);
        }
      }
      hypotheses.add(hypothesis);
      rounds++;
      if (completeDeep) deepRounds++;
    }
    if (finished === null || failure) {
      timedOut = finished === null;
      discarded += groups.length - completed.length;
      pool.cancel();
      if (failure) warnings.add(`An incomplete round was discarded: ${failure.message}`);
      break;
    }
  }
  if (!rounds) throw new Error('The deadline passed before every action was compared.');
  if (!deepRounds) for (const root of roots) root.rows = root.shallow;
  const depth = deepRounds ? 2 : 1;
  const result = core.summarize(roots, payload.risk ?? .35, depth);
  return {...result, diagnostics: {engine: 'official-pokemon-showdown', workers: pool.size,
    workers_used: workersUsed.size, worker_thread_ids: [...workersUsed], nodes, depth, scenarios: rounds, deep_scenarios: deepRounds,
    hypotheses: hypotheses.size, conditioned_hypotheses: fitted.order,
    observed_damage_samples: fitted.fitted, observed_choice_samples: fitted.choices,
    hypothesis_errors: fitted.errors, discarded_rounds: discarded, deadline_reached: timedOut,
    root_actions: roots.length, elapsed_ms: Date.now() - began, time_budget_ms: payload.timeBudgetMs,
    chance_samples: roots[0]?.samples.length || 0, strategy: payload.strategy?.archetype || 'unspecified',
    forced_loss_trade: result.forcedLoss, warnings: [...warnings],
    uncertainty: 'Complete comparable action matrices; hidden sets and chance remain uncertain.'}};
}

async function start(core) {
  const capacity = Math.max(1, Math.min(10, (os.availableParallelism?.() || os.cpus().length) - 2));
  const pool = new Pool(Math.min(capacity, Number(process.env.SHOWDOWN_SEARCH_WORKERS)));
  try { await Promise.all(pool.slots.map(slot => slot.readyPromise)); }
  catch (error) { process.stdout.write(JSON.stringify({ready: false, error: error.message}) + '\n'); process.exit(1); }
  process.stdout.write(JSON.stringify({ready: true, simulator: 'pokemon-showdown', protocol: 1, workers: pool.size}) + '\n');
  const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  let busy = false;
  input.on('close', () => process.exit(0));
  input.on('line', async line => {
    if (busy) { process.stdout.write(JSON.stringify({error: 'Simulator busy with another analysis.'}) + '\n'); return; }
    busy = true;
    try {
      const payload = JSON.parse(line);
      const result = payload.op === 'search' ? await parallelSearch(pool, core, payload) : core.dispatch(payload);
      if (payload.op === 'health') result.workers = pool.size;
      process.stdout.write(JSON.stringify(result) + '\n');
    } catch (error) { process.stdout.write(JSON.stringify({error: String(error.message)}) + '\n'); }
    finally { busy = false; }
  });
}

module.exports = {start};
