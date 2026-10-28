import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { detectedStyle, roleBrief } from './team-builder';

const TEAM = 'Mimikyu @ Life Orb\nAbility: Disguise\nIVs: 0 Atk\n- Shadow Sneak';

describe('manual team analysis', () => {
  beforeEach(() => vi.resetModules());
  afterEach(() => vi.unstubAllGlobals());
  it('keeps compact role labels and the inferred style separate from the chosen plan', () => {
    expect(roleBrief({ Mimikyu: ['setup_sweeper', 'offensive_attacker'], Ribombee: ['hazard_setter'] })).toBe('Setup · Attackers · Hazards');
    expect(detectedStyle({ archetype: 'stall', inferred_archetype: 'weather', label: 'Stall' })).toBe('Weather');
  });
  it('analyzes a draft without saving, replacing, or recommending a team', async () => {
    const profile = { archetype: 'hyper_offense', label: 'Hyper offense' };
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ token: 'local-token' })))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, profile })));
    vi.stubGlobal('fetch', fetcher);
    const { analyzeTeam } = await import('./team-builder');
    expect(await analyzeTeam(TEAM)).toEqual(profile);
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/api/session', '/api/team/analyze']);
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ text: TEAM, archetype: 'auto' });
  });
});
