export interface Config {
  username: string; format_id: string; mode: 'ladder' | 'challenge' | 'accept';
  opponent: string; battles: number; autoplay: boolean; server: 'showdown' | 'local' | 'custom';
  websocket_url: string; authentication_url: string; risk: number;
  allow_tera: boolean; auto_update_meta: boolean; learning_enabled: boolean;
  matchmaking_timeout: number; archetype: Archetype;
  concurrent_battles: number; cpu_level: 'low' | 'medium' | 'high'; avatar: string; engine?: 'foulplay' | 'hybrid' | 'neural'; [key: string]: unknown;
}
export type Stats = Record<'hp' | 'atk' | 'def' | 'spa' | 'spd' | 'spe', number>;
export interface Member {
  species: string; nickname: string; item: string; ability: string; nature: string;
  evs: Stats; ivs: Stats; moves: string[]; tera_type: string; level: number;
  gender: string; shiny: boolean; types?: string[]; sprite?: string;
}
export type Archetype = 'auto' | 'hyper_offense' | 'bulky_offense' | 'balance' | 'semi_stall' | 'stall' | 'weather';
export interface Team { name: string; label?: string; text: string; members: Member[]; format_id?: string; archetype?: Archetype; profile?: TeamProfile; error?: string }
export interface TeamProfile {
  archetype: Archetype; inferred_archetype?: Archetype; label: string; roles?: unknown; lead_species?: string;
  win_conditions?: string[]; reason?: string; confidence?: number;
}
export interface TeamPreferences { format_id: string; archetype: Archetype }
export interface BattleRecord {
  battle_id: string; storage_id?: string; battle_tag?: string; server_namespace?: string;
  username?: string; ended_at: string; format_id: string; opponent: string;
  result: 'win' | 'loss' | 'tie'; rating_before?: number | null; rating_after?: number | null;
  turns?: number; summary?: string; replay_path?: string; pokemon?: string[];
  opponent_pokemon?: string[]; opponent_rank?: number | null; own_rank?: number | null;
}
export interface LiveBattle {
  battle: string; format_id: string; started_at: number; turn: number; opponent: string;
  own_team: string[]; foe_team: string[]; own_alive?: number; foe_alive?: number;
  rating?: number | null; opponent_rating?: number | null;
  scout?: { games: number; summary: string; archetype?: string; team_games?: number; record?: Record<string, number> };
  win_chance?: number | null; opponent_ladder?: { rank: number | null; elo?: number; gxe?: number } | null; own_ladder?: { rank: number | null; elo?: number; gxe?: number } | null;
  field?: { own?: Record<string, number>; foe?: Record<string, number>; weather?: string[]; fields?: string[] };
}
export interface State {
  config: Config; teams: Team[]; selected_team: string; format_choices: Record<string, string>;
  history: BattleRecord[]; ratings: Record<string, number | null>; status: string; running: boolean;
  metrics: { cpu: number | null; latency_ms: number | null; last_sync: number | string | null };
  decision?: unknown;
  performance?: { wins: number; losses: number; ties?: number; winrate: number };
  error?: string; status_message?: string; live?: LiveBattle[]; account_saved?: boolean;
  search_health?: { full: number; total: number }; team_records?: Record<string, { wins: number; losses: number }>; team_elo?: { delta: number | null; games: number }; version?: string;
}
export interface ApiResult { ok?: boolean; error?: string; state?: State; token?: string; report?: Record<string, unknown> }
export const DEFAULT_CONFIG: Config = {
  username: '', format_id: 'gen9ou', mode: 'ladder', opponent: '', battles: 1, autoplay: false,
  server: 'showdown', websocket_url: '', authentication_url: '', risk: 0.35,
  allow_tera: true, auto_update_meta: true, learning_enabled: true, matchmaking_timeout: 300,
  archetype: 'auto', concurrent_battles: 1, cpu_level: 'medium', avatar: '',
};
export const INITIAL_STATE: State = {
  config: DEFAULT_CONFIG, teams: [], selected_team: '', format_choices: {}, history: [],
  ratings: {}, status: 'Idle', running: false, metrics: { cpu: null, latency_ms: null, last_sync: null },
};
