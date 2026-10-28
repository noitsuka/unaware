import { post } from './api';
import type { ApiResult, Archetype, TeamProfile } from './types';

export const ARCHETYPES: Record<Archetype, string> = {
  auto: 'Auto', hyper_offense: 'Hyper offense', bulky_offense: 'Bulky offense',
  balance: 'Balance', semi_stall: 'Semi-stall', stall: 'Stall', weather: 'Weather',
};
const ROLE_LABELS: Record<string, string> = {
  setup: 'Setup', setup_sweeper: 'Setup', attacker: 'Attackers', offensive_attacker: 'Attackers',
  bulky_attacker: 'Bulky attackers', hazards: 'Hazards', hazard_setter: 'Hazards',
  web_setter: 'Sticky Web', pivot: 'Pivots', recovery: 'Recovery', wall: 'Walls',
  hazard_removal: 'Removal', removal: 'Removal', screens: 'Screens', suicide_lead: 'Lead', lead: 'Lead',
  status: 'Status', status_spreader: 'Status', speed: 'Speed', speed_control: 'Speed control',
  priority: 'Priority', weather_setter: 'Weather', anti_setup: 'Anti-setup',
  phazing: 'Phazing', wallbreaker: 'Wallbreaker', breaker: 'Wallbreaker',
  cleaner: 'Cleaner', support: 'Support',
};

export function detectedStyle(profile: TeamProfile): string {
  const inferred = profile.inferred_archetype ?? profile.archetype;
  return ARCHETYPES[inferred] || profile.label;
}

export async function analyzeTeam(text: string, signal?: AbortSignal): Promise<TeamProfile> {
  const result = await post<ApiResult & { profile?: TeamProfile }>('team/analyze', { text, archetype: 'auto' }, { signal });
  if (!result.profile || typeof result.profile.label !== 'string') throw new Error('Could not identify the team style.');
  return result.profile;
}

export function explain(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!value || typeof value !== 'object') return '';
  const detail = value as Record<string, unknown>;
  return ['reason', 'detail', 'description', 'label', 'name', 'species', 'role'].map(key => detail[key])
    .filter((item): item is string => typeof item === 'string' && item.length > 0).join(' · ');
}

export function roleBrief(roles: unknown): string {
  if (!roles || typeof roles !== 'object') return '';
  const all = Array.isArray(roles) ? roles : Object.values(roles);
  const labels = all.flatMap(value => Array.isArray(value) ? value : typeof value === 'object' && value ?
    (Array.isArray((value as { roles?: unknown }).roles) ? (value as { roles: unknown[] }).roles : [explain(value)]) : [value])
    .filter((value): value is string => typeof value === 'string' && value.length > 0);
  return [...new Set(labels.map(label => ROLE_LABELS[label] || label))].slice(0, 3).join(' · ');
}
