import { describe, expect, it } from 'vitest';
import { performanceTotals } from './performance';
import { INITIAL_STATE, type BattleRecord } from './types';

const record = (result: BattleRecord['result'], format_id = 'gen9ou', username = 'Test'): BattleRecord => ({
  battle_id: 'example', ended_at: '', opponent: '', result, format_id, username,
});
describe('performance totals', () => {
  it('uses complete backend counts instead of the limited visible history and excludes ties', () => {
    expect(performanceTotals({ ...INITIAL_STATE, history: [record('loss')], performance: { wins: 1500, losses: 500, ties: 40, winrate: 75 } })).toEqual({ wins: 1500, losses: 500, winrate: 75 });
  });
  it('keeps empty records at zero and scopes legacy fallback by account and tier', () => {
    expect(performanceTotals(INITIAL_STATE)).toEqual({ wins: 0, losses: 0, winrate: 0 });
    expect(performanceTotals({ ...INITIAL_STATE, config: { ...INITIAL_STATE.config, username: 'test' }, history: [record('win'), record('tie'), record('loss', 'gen9ru'), record('loss', 'gen9ou', 'Other')] })).toEqual({ wins: 1, losses: 0, winrate: 100 });
  });
});
