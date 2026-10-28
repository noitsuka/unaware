import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BarChart3, ChevronRight, CircleDot, Copy, Eye, FileText, LayoutGrid, LoaderCircle, Maximize2, Minus, MoreHorizontal, Pencil, Play, Plus, Radio, RotateCcw, Settings, Square, Swords, Timer, Trash2, Users, X } from 'lucide-react';
import type { ApiResult, BattleRecord, Config, LiveBattle, State, Team, TeamPreferences } from './types';
import { INITIAL_STATE } from './types';
import { cancelPendingRequests, isCancelled, post, readState } from './api';
import { performanceTotals } from './performance';
import { AccountDialog, BattleDialog, TeamDialog } from './dialogs';
import { IconButton, Modal, PokemonIcon, TierSelect, Toggle } from './components';
import { battleId } from './team';
import { TeamLibrary } from './library';
import { AVATARS, avatarUrl } from './avatars';
import { FieldSummary } from './field';
import { useCountUp } from './countup';

declare global { interface Window { pywebview?: { api: { minimize: () => Promise<void>; fullscreen: () => Promise<boolean>; close: () => Promise<void> } } } }

// The layout is designed for 1600x900 and scales as one piece, so a smaller
// window shows the same screen proportionally instead of breaking apart.
const BASE_WIDTH = 1600, BASE_HEIGHT = 900;
function useUiScale() {
  const read = () => Math.min(1, window.innerWidth / BASE_WIDTH, window.innerHeight / BASE_HEIGHT);
  const [scale, setScale] = useState(read);
  useEffect(() => {
    const update = () => setScale(read());
    window.addEventListener('resize', update);
    return () => window.removeEventListener('resize', update);
  }, []);
  return scale;
}

type View = 'battle' | 'live' | 'teams' | 'settings';

function time(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value.slice(0, 16);
  return date.toLocaleString('en-GB', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }).replace(',', '');
}

function elapsed(start: number, now: number): string {
  const seconds = Math.max(0, Math.floor(now / 1000 - start));
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

const formatName = (formats: Record<string, string>, id: string) => (formats[id] || id).replace('[Gen 9]', 'Gen 9');

function Sprite({ id, species }: { id?: string; species: string }) {
  const [failed, setFailed] = useState(false);
  if (!id || failed) return <PokemonIcon species={species} />;
  return <img className="roster-sprite" src={`https://play.pokemonshowdown.com/sprites/home/${id}.png`} alt="" loading="lazy" onError={() => setFailed(true)} />;
}

// Showdown's avatar picker: a button with the current avatar opens every official icon (no names).
function AvatarSetting({ value, disabled, onSave }: { value: string; disabled: boolean; onSave: (avatar: string) => void }) {
  const [open, setOpen] = useState(false);
  const [broken, setBroken] = useState<ReadonlySet<string>>(() => new Set());
  const hide = (name: string) => setBroken(previous => new Set(previous).add(name));
  return <div className="setting">
    <div><strong>Avatar</strong><span>Pick any Showdown avatar. While the bot is online it changes right away.</span></div>
    <button type="button" className="avatar-current" disabled={disabled} aria-label="Choose avatar" onClick={() => setOpen(true)}>
      {value ? <img src={avatarUrl(value)} alt="" /> : <Users size={28} />}
    </button>
    {open && <Modal title="Choose avatar" className="avatar-modal" onClose={() => setOpen(false)}>
      <div className="avatar-grid" role="listbox" aria-label="Avatars">
        {AVATARS.filter(name => !broken.has(name)).map(name => <button key={name} type="button" role="option" aria-selected={name === value} aria-label={name}
          className={name === value ? 'on' : ''} disabled={disabled} onClick={() => { if (name !== value) onSave(name); setOpen(false); }}>
          <img src={avatarUrl(name)} alt="" loading="lazy" decoding="async" onError={() => hide(name)} /></button>)}
      </div>
    </Modal>}
  </div>;
}

function TypeBadge({ type }: { type: string }) {
  return <span className={'type-badge type-' + type.toLowerCase()}>{type.toUpperCase()}</span>;
}

const HistoryList = memo(function HistoryList({ records, formats, onSelect, onReplay }: {
  records: BattleRecord[]; formats: Record<string, string>;
  onSelect: (record: BattleRecord) => void; onReplay: (record: BattleRecord) => void;
}) {
  if (!records.length) return <p className="empty-note">No battles yet.</p>;
  return <ul className="history-list">
    {records.map(record => {
      const delta = typeof record.rating_after === 'number' && typeof record.rating_before === 'number' ? Math.round(record.rating_after - record.rating_before) : null;
      const mon = record.opponent_pokemon?.[0] ?? record.pokemon?.[0];
      return <li key={battleId(record)} className={record.result}>
        <span className="result-letter">{{ win: 'W', loss: 'L', tie: 'T' }[record.result]}</span>
        <button className="history-main" onClick={() => onSelect(record)}>
          <span className="history-first-line"><span className="history-opponent">{record.opponent || '-'}</span><time>{time(record.ended_at)}</time></span>
          <span className="history-second-line">{formatName(formats, record.format_id)}{typeof record.rating_after === 'number' ? ` • ${Math.round(record.rating_after)}` : ''}{delta === null ? '' : ` (${delta > 0 ? '+' : ''}${delta})`}{record.turns ? ` • ${record.turns} turns` : ''}</span>
          {(typeof record.own_rank === 'number' || typeof record.opponent_rank === 'number') && <span className="history-ranks"><RankTag who="you" rank={record.own_rank} /><RankTag who="opp" rank={record.opponent_rank} /></span>}
        </button>
        <div className="history-mon">{mon ? <PokemonIcon species={mon} small /> : <CircleDot size={17} strokeWidth={1.2} aria-hidden="true" />}</div>
        <IconButton label={`Open replay vs ${record.opponent}`} disabled={!record.replay_path} onClick={() => onReplay(record)}><FileText size={16} strokeWidth={1.4} /></IconButton>
      </li>;
    })}
  </ul>;
});

function LadderRank({ info }: { info?: { rank: number | null; elo?: number; gxe?: number } | null }) {
  if (!info) return null;
  return <span className={'live-rank' + (info.rank ? ' top' : '')} title={info.rank ? `Elo ${info.elo} • GXE ${info.gxe}%` : 'Not in the top 500 of this ladder'}>
    {info.rank ? `#${info.rank} on ladder` : 'outside top 500'}</span>;
}

function RankTag({ who, rank }: { who: string; rank?: number | null }) {
  if (typeof rank !== 'number') return null;  // outside the top 500, or not recorded
  return <span className="history-rank top" title={`${who}: #${rank} in the top 500 when the battle started`}>{who} #{rank}</span>;
}

function LiveCard({ live, username, formats, now, onWatch }: { live: LiveBattle; username: string; formats: Record<string, string>; now: number; onWatch: (tag: string) => void }) {
  return <article className="live-card">
    <header className="live-meta">
      <span className="live-dot">LIVE</span><span>{formatName(formats, live.format_id)}</span>
      <span>Turn {live.turn || 0}</span><span className="live-clock"><Timer size={13} />{elapsed(live.started_at, now)}</span>
      <BarChart3 size={14} className="live-signal" aria-hidden="true" />
    </header>
    <div className="live-body">
      <div className="live-side"><span className="live-name">{username || 'Bot'}{live.rating ? <em> {Math.round(live.rating)}</em> : null}</span>
        <LadderRank info={live.own_ladder} /><div className="live-team">{live.own_team.map((mon, i) => <PokemonIcon key={mon + i} species={mon} small />)}</div></div>
      <span className="live-vs">VS</span>
      <div className="live-side"><span className="live-name">{live.opponent || 'Opponent'}{live.opponent_rating ? <em> {live.opponent_rating}</em> : null}</span>
        <LadderRank info={live.opponent_ladder} /><div className="live-team">{live.foe_team.map((mon, i) => <PokemonIcon key={mon + i} species={mon} small />)}</div></div>
      {typeof live.win_chance === 'number' && <div className="live-win" title="Estimated from remaining Pokémon and HP, adjusted by the neural network over likely opponent sets; smoothed between turns.">
        <span>Win chance</span><b className={live.win_chance >= 55 ? 'up' : live.win_chance <= 45 ? 'down' : ''}>{live.win_chance}%</b>
        <i><s style={{ width: `${Math.max(2, Math.min(98, live.win_chance))}%` }} /></i>
      </div>}
      <FieldSummary field={live.field} />
      {live.scout && <p className="live-scout" title={live.scout.summary}>{live.scout.games ? live.scout.summary : 'No history for this player.'}</p>}
      <button className="watch-button" onClick={() => onWatch(live.battle)} title="Open the room in your browser (play.pokemonshowdown.com)"><Eye size={17} />WATCH</button>
    </div>
  </article>;
}

function LivePanel({ state, now, onWatch, large = false }: { state: State; now: number; onWatch: (tag: string) => void; large?: boolean }) {
  const live = state.live ?? [];
  return <section className={'card live-panel' + (large ? ' large' : '')} aria-label="Battles in progress">
    <header className="card-header"><h2>BATTLES IN PROGRESS</h2><span className={'live-badge' + (live.length ? ' on' : '')}>LIVE</span><span className="live-count">{live.length}</span></header>
    <div className="live-list">
      {live.length ? live.map(item => <LiveCard key={item.battle} live={item} username={state.config.username} formats={state.format_choices} now={now} onWatch={onWatch} />)
        : <p className="empty-note">{state.running ? 'Searching for an opponent...' : 'No battles in progress. Press START.'}</p>}
    </div>
  </section>;
}

export default function App() {
  const [state, setState] = useState<State>(INITIAL_STATE);
  const [view, setView] = useState<View>('battle');
  const [pending, setPending] = useState(false);
  const [launching, setLaunching] = useState(false);
  const operation = useRef<AbortController | null>(null);
  const connection = useRef<EventSource | null>(null);
  const mounted = useRef(true);
  const [alert, setAlert] = useState('');
  const [accountOpen, setAccountOpen] = useState(false);
  const [editor, setEditor] = useState<Team | 'new' | null>(null);
  const [deleting, setDeleting] = useState<Team | null>(null);
  const [clearingHistory, setClearingHistory] = useState(false);
  const [battle, setBattle] = useState<BattleRecord | null>(null);
  const [autoplay, setAutoplay] = useState(false);
  const [now, setNow] = useState(Date.now());
  const [maximized, setMaximized] = useState(false);
  const [teamsTab, setTeamsTab] = useState<'mine' | 'library'>('mine');
  const [resettingLearning, setResettingLearning] = useState(false);
  const scale = useUiScale();
  const eloArt = useRef<HTMLDivElement>(null);
  const [banner, setBanner] = useState<{ key: string; result: 'win' | 'loss' | 'tie'; delta: number | null; opponent: string } | null>(null);
  const lastResult = useRef<string | null>(null);
  const openedAt = useRef(new Date().toISOString());
  const password = useRef('');
  const startAfterAccount = useRef(false);
  const lastError = useRef('');
  const current = useRef(state);
  current.current = state;

  const hasLive = Boolean(state.live?.length);
  useEffect(() => {
    if (!hasLive) return;
    const timer = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(timer);
  }, [hasLive]);

  const applyState = useCallback((next: State) => {
    setAutoplay(Boolean(next.config.autoplay));
    if (next.error && next.error !== lastError.current) setAlert(next.error);
    lastError.current = next.error || '';
    const confirmed = { ...INITIAL_STATE, ...next, config: { ...INITIAL_STATE.config, ...next.config }, metrics: { ...INITIAL_STATE.metrics, ...next.metrics } };
    current.current = confirmed;
    setState(confirmed);
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    readState(controller.signal).then(next => {
      if (controller.signal.aborted) return;
      applyState(next);
      const stream = new EventSource('/api/events');
      connection.current = stream;
      stream.onmessage = event => {
        if (controller.signal.aborted) return;
        try { const data = JSON.parse(event.data); if (data.state) applyState(data.state); else if (data.config && data.teams) applyState(data); }
        catch { /* Keep the last confirmed state if an event is incomplete. */ }
      };
    }).catch(problem => { if (!controller.signal.aborted && !isCancelled(problem)) setAlert(problem.message); });
    const cleanup = () => {
      mounted.current = false;
      controller.abort();
      operation.current?.abort();
      connection.current?.close();
      connection.current = null;
      cancelPendingRequests();
    };
    window.addEventListener('beforeunload', cleanup);
    return () => { window.removeEventListener('beforeunload', cleanup); cleanup(); };
  }, [applyState]);

  const perform = async (task: (signal: AbortSignal) => Promise<ApiResult>, interrupt = false): Promise<boolean> => {
    if (operation.current && !interrupt) return false;
    if (interrupt) {
      operation.current?.abort();
      cancelPendingRequests();
    }
    const controller = new AbortController();
    operation.current = controller;
    setPending(true);
    try {
      const result = await task(controller.signal);
      const next = result.state ?? await readState(controller.signal);
      if (controller.signal.aborted || !mounted.current) return false;
      applyState(next);
      return true;
    } catch (problem) {
      if (!controller.signal.aborted && !isCancelled(problem) && mounted.current) setAlert(problem instanceof Error ? problem.message : String(problem));
      return false;
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        if (mounted.current) setPending(false);
      }
    }
  };

  const close = () => {
    operation.current?.abort();
    cancelPendingRequests();
    connection.current?.close();
    password.current = '';
    void window.pywebview?.api.close();
  };

  const selected = state.teams.find(team => team.name === state.selected_team) ?? state.teams[0];
  const random = state.config.format_id === 'gen9randombattle';
  const canStop = state.running || launching;
  const disabled = pending || canStop;
  const saveConfig = (changes: Partial<Config>) => perform(signal => post('config', { config: { ...current.current.config, ...changes, selected_team: current.current.selected_team } }, { signal }));
  const selectTeam = (team: Team) => { void perform(signal => post('config', { config: { ...state.config, selected_team: team.name, format_id: team.format_id ?? state.config.format_id, archetype: team.archetype ?? 'auto' } }, { signal })); };
  const chooseTier = (format_id: string) => { void perform(signal => post('config', { config: { ...state.config, format_id, selected_team: selected?.name ?? '' } }, { signal })); };

  const launch = async (config: Config, secret: string) => {
    setLaunching(true);
    const team = current.current.teams.find(item => item.name === current.current.selected_team) ?? current.current.teams[0];
    await perform(signal => post('start', { ...config, password: secret, selected_team: team?.name ?? '', team: config.format_id === 'gen9randombattle' ? '' : team?.text ?? '', battles: 1, autoplay }, { signal }));
    if (mounted.current) setLaunching(false);
  };

  const start = async () => {
    if (current.current.running || launching) {
      setLaunching(false);
      await perform(signal => post('stop', {}, { signal }), true);
      return;
    }
    const config = current.current.config;
    if (!config.username.trim()) { startAfterAccount.current = true; setAccountOpen(true); return; }
    await launch(config, password.current);
  };

  const saveAccount = async (config: Config, secret: string) => {
    const continueToBattle = startAfterAccount.current;
    const saved = await perform(signal => post('config', { config: { ...config, password: secret, selected_team: current.current.selected_team } }, { signal }));
    if (!saved) return;
    password.current = secret;
    setAccountOpen(false);
    startAfterAccount.current = false;
    if (continueToBattle) await launch(config, secret);
  };

  const saveTeam = async (name: string, text: string, original_name?: string, preferences?: TeamPreferences) => {
    if (await perform(signal => post('team', { name, text, original_name, ...preferences }, { signal }))) setEditor(null);
  };
  const copyTeam = (team: Team) => {
    const base = team.name.replace(/\.txt$/i, '');
    const names = new Set(state.teams.map(item => item.name));
    let index = 1, name = `${base} (copy)`;
    while (names.has(name + '.txt')) name = `${base} (copy ${++index})`;
    void perform(signal => post('team', { name, text: team.text, format_id: team.format_id ?? state.config.format_id, archetype: team.archetype ?? 'auto' }, { signal }));
  };
  const openReplay = useCallback((record: BattleRecord) => { void perform(signal => post('open-replay', { battle_id: battleId(record) }, { signal })); }, []);
  const watch = useCallback((tag: string) => { void perform(signal => post('watch', { battle: tag }, { signal }), true); }, []);
  const recover = async () => { await perform(signal => post('recover', {}, { signal }), true); };
  // One more ladder battle now, while the running ones continue.
  const extraBattle = async () => { await perform(signal => post('extra-battle', {}, { signal })); };
  const liveCount = state.live?.length ?? 0;
  const changeAutoplay = (checked: boolean) => {
    setAutoplay(checked);
    void perform(signal => post('autoplay', { enabled: checked }, { signal })).then(saved => { if (!saved) setAutoplay(Boolean(current.current.config.autoplay)); });
  };
  const performance = useMemo(() => performanceTotals(state), [state.history, state.performance, state.config.format_id, state.config.username]);
  const rating = state.ratings[state.config.format_id];
  // Elo won or lost with the selected team in this tier (backend: team_elo).
  const teamDelta = state.team_elo?.delta ?? null;
  // A battle that ends while the app is open: a short flash on the Elo panel.
  const newest = state.history[0];
  useEffect(() => {
    if (!newest) return;
    const key = battleId(newest);
    if (lastResult.current === null) { lastResult.current = key; return; }
    if (lastResult.current === key) return;
    lastResult.current = key;
    const delta = typeof newest.rating_after === 'number' && typeof newest.rating_before === 'number' ? Math.round(newest.rating_after - newest.rating_before) : null;
    setBanner({ key, result: newest.result, delta, opponent: newest.opponent || '' });  // drives the Elo flash
  }, [newest]);
  useEffect(() => {
    if (!banner) return;
    const timer = setTimeout(() => setBanner(null), 3800);
    return () => clearTimeout(timer);
  }, [banner]);
  const shownRating = useCountUp(typeof rating === 'number' && Number.isFinite(rating) ? Math.round(rating) : null);
  const tierHistory = useMemo(() => state.history.filter(item => item.format_id === state.config.format_id), [state.history, state.config.format_id]);
  const recentWins = tierHistory.slice(0, 20).filter(item => item.result === 'win').length;
  const recentLosses = tierHistory.slice(0, 20).filter(item => item.result === 'loss').length;
  const session = tierHistory.filter(item => item.ended_at >= openedAt.current);
  const sessionWins = session.filter(item => item.result === 'win').length;
  const sessionLosses = session.filter(item => item.result === 'loss').length;
  const status = state.status || (state.running ? 'Playing' : 'Idle');
  const statusLabel: Record<string, string> = { Idle: 'Idle', Starting: 'Starting', Connecting: 'Connecting', Waiting: 'Searching', Playing: 'Playing', Stopping: 'Stopping', Recovering: 'Recovering', Error: 'Error' };
  const online = state.running && status !== 'Error';

  const nav: [View, string, typeof Swords][] = [['battle', 'BATTLE', Swords], ['live', 'LIVE', Radio], ['teams', 'TEAMS', Users], ['settings', 'SETTINGS', Settings]];

  const teamPanel = <section className="card team-panel" aria-label="Your team">
    <header className="card-header"><h2>YOUR TEAM</h2></header>
    <div className="team-toolbar">
      <div className="select-wrap team-select-wrap"><select aria-label="Selected team" value={selected?.name ?? ''} disabled={disabled || random} onChange={event => { const team = state.teams.find(item => item.name === event.target.value); if (team) selectTeam(team); }}>
        {state.teams.length ? state.teams.map(team => <option key={team.name} value={team.name}>{team.name.replace(/\.txt$/i, '')}{team.profile?.label ? ` (${team.profile.label})` : ''}</option>) : <option value="">No teams</option>}
      </select></div>
      <div className="tool-group">
        <IconButton label="New team" disabled={disabled} onClick={() => setEditor('new')}><Plus size={17} /></IconButton>
        <IconButton label="Edit team" disabled={disabled || !selected} onClick={() => selected && setEditor(selected)}><Pencil size={15} /></IconButton>
        <IconButton label="Duplicate team" disabled={disabled || !selected} onClick={() => selected && copyTeam(selected)}><Copy size={15} /></IconButton>
        <IconButton label="Delete team" disabled={disabled || !selected} onClick={() => selected && setDeleting(selected)}><Trash2 size={15} /></IconButton>
      </div>
    </div>
    <ul className="roster-list">
      {random ? <li className="roster-empty">Random Battle teams are generated by the server.</li> : selected?.members.map((member, index) => <li key={`${member.species}-${index}`}>
        <span className="grip" aria-hidden="true">⋮⋮</span><Sprite id={member.sprite} species={member.species} />
        <div className="roster-info"><strong>{member.nickname || member.species}</strong><span className="badges">{(member.types ?? []).map(type => <TypeBadge key={type} type={type} />)}</span></div>
        <IconButton label={`Edit ${member.species}`} disabled={disabled} onClick={() => setEditor(selected)}><MoreHorizontal size={18} /></IconButton>
      </li>)}
    </ul>
    <button className="wide-button" onClick={() => setView('teams')}><LayoutGrid size={19} /><span>MANAGE TEAMS</span><ChevronRight size={18} /></button>
  </section>;

  const formatPanel = <section className="card format-panel" aria-label="Format">
    <header className="card-header"><h2>FORMAT</h2></header>
    <TierSelect options={state.format_choices} value={state.config.format_id} onChange={chooseTier} disabled={disabled} />
    <div className={'elo-art' + (banner ? ' flash-' + banner.result : '')} ref={eloArt}>
      <div className="elo-ring" aria-hidden="true" />
      <span className="elo-label">CURRENT ELO</span>
      <strong className="elo-value">{shownRating ?? '-'}</strong>
      {teamDelta !== null && <span className={'elo-delta ' + (teamDelta >= 0 ? 'up' : 'down')} title={`Total with this team over ${state.team_elo?.games} rated battles`}>{teamDelta >= 0 ? '▲' : '▼'} {teamDelta >= 0 ? '+' : ''}{teamDelta} <small>with this team</small></span>}
      {selected?.profile && !random && <span className="elo-style">Style: {selected.profile.label}{(() => { const r = state.team_records?.[selected.name]; return r ? ` • ${r.wins}W ${r.losses}L (${Math.round(100 * r.wins / Math.max(1, r.wins + r.losses))}%)` : ''; })()}</span>}
    </div>
    <div className="form-row" title="Last 20 battles in this format">
      <span className="form-label">LAST {Math.min(20, tierHistory.length) || 20}</span>
      <span className="form-dots">{tierHistory.length ? <><b className="wins">{recentWins}W</b> <b className="losses">{recentLosses}L</b></> : <em>no battles</em>}</span>
      <span className="session">Session <b className="wins">{sessionWins}W</b> <b className="losses">{sessionLosses}L</b></span>
    </div>
    <button className={'start-button' + (canStop ? ' playing' : '')} disabled={pending && !canStop} onClick={() => { void start(); }}>
      
      {pending ? <LoaderCircle size={24} className="animate-spin" /> : canStop ? <Square size={22} /> : <Swords size={26} />}
      {canStop ? 'STOP' : 'START'}
    </button>
    {state.running && state.config.mode === 'ladder' && <button className="extra-battle-button" disabled={pending || liveCount < 1 || liveCount >= 5}
      title={liveCount < 1 ? 'Available once a battle is in progress' : 'Starts another ladder battle without stopping the running ones'} onClick={() => { void extraBattle(); }}>
      <Plus size={18} />NEW BATTLE{liveCount > 0 && <span className="extra-battle-count">{liveCount} in progress</span>}
    </button>}
    <div className="autoplay-row">
      <Play size={18} className="autoplay-icon" /><span>AUTOPLAY</span>
      <Toggle label="Autoplay" checked={autoplay} onChange={changeAutoplay} disabled={pending} />
      <span className="current-status" title={state.status_message || status}><i className={'dot ' + (online ? 'on' : status === 'Error' ? 'error' : '')} />Status: <b>{statusLabel[status] ?? status}</b></span>
    </div>
    {(state.search_health?.total ?? 0) > 0 && (() => {
      const health = state.search_health!, share = health.full / Math.max(1, health.total);
      return <p className={'search-health ' + (share >= .95 ? 'good' : share >= .8 ? 'warn' : 'bad')} title="Moves decided by the main engine this session; the rest were leads, forced switches or fallbacks">
        Main engine: <b>{Math.round(share * 100)}%</b> of moves ({health.full}/{health.total})</p>;
    })()}
    <p className="slots-note">{state.config.concurrent_battles > 1 ? `${state.config.concurrent_battles} concurrent battles` : '1 battle at a time'} • Battles open in your browser</p>
  </section>;

  const performanceStrip = <dl className="performance-strip" aria-label="Results in the selected format">
    <div><dt>Wins</dt><dd className="wins">{performance.wins}</dd></div>
    <div><dt>Losses</dt><dd className="losses">{performance.losses}</dd></div>
    <div><dt>Winrate</dt><dd>{performance.winrate.toFixed(1)}%</dd></div>
  </dl>;

  return <div className="desktop-shell" style={{ width: `${100 / scale}vw`, height: `${100 / scale}vh`, transform: scale < 1 ? `scale(${scale})` : undefined }}>
    <div className="vignette" aria-hidden="true" />
    <header className="app-header">
      {!maximized && <div className="drag-zone pywebview-drag-region" aria-hidden="true" />}
      <div className="brand"><span className="brand-mark" aria-hidden="true"><img className="brand-logo" src="/assets/art/logo.svg" alt="" /></span><div><h1>UN<span>AWARE</span></h1><small>v{state.version ?? '1.0.0'}</small></div></div>
      <div className="header-right">
        <span className={'bot-pill' + (online ? ' on' : '')}><i />{online ? 'BOT ONLINE' : 'BOT OFFLINE'}<BarChart3 size={16} /></span>
        <div className="window-controls">
          <IconButton label="Minimize" onClick={() => { void window.pywebview?.api.minimize(); }}><Minus size={17} /></IconButton>
          <IconButton label={maximized ? 'Restore' : 'Maximize'} onClick={() => { void window.pywebview?.api.fullscreen().then(state => setMaximized(Boolean(state))); }}><Maximize2 size={15} /></IconButton>
          <IconButton label="Close" onClick={close}><X size={18} /></IconButton>
        </div>
      </div>
    </header>

    <nav className="sidebar" aria-label="Navigation">
      {nav.map(([key, label, Icon]) => <button key={key} className={'nav-item' + (view === key ? ' active' : '')} onClick={() => setView(key)}><Icon size={24} strokeWidth={1.6} /><span>{label}</span>{key === 'live' && (state.live?.length ?? 0) > 0 && <em className="nav-count">{state.live?.length}</em>}</button>)}
    </nav>

    <main className={'content view-' + view} id="main">
      {view === 'battle' && <>{teamPanel}{formatPanel}<LivePanel state={state} now={now} onWatch={watch} /></>}
      {view === 'live' && <>
        <LivePanel state={state} now={now} onWatch={watch} large />
        <section className="card history-panel" aria-label="History">
          <header className="card-header"><h2>HISTORY</h2><IconButton label="Clear history" disabled={disabled || !state.history.length} onClick={() => setClearingHistory(true)}><Trash2 size={16} /></IconButton></header>
          {performanceStrip}
          <HistoryList records={state.history} formats={state.format_choices} onSelect={setBattle} onReplay={openReplay} />
        </section>
      </>}
      {view === 'teams' && <section className="card teams-view" aria-label="Teams">
        <header className="card-header"><h2>TEAMS</h2>
          <div className="segmented wide tabs"><button className={teamsTab === 'mine' ? 'on' : ''} onClick={() => setTeamsTab('mine')}>My teams</button><button className={teamsTab === 'library' ? 'on' : ''} onClick={() => setTeamsTab('library')}>Library</button></div>
          <button className="small-button" disabled={disabled} onClick={() => setEditor('new')}><Plus size={15} />New / paste</button></header>
        {teamsTab === 'library' ? <TeamLibrary disabled={disabled} formats={state.format_choices} initialFormat={state.config.format_id === 'gen9randombattle' ? 'gen9ou' : state.config.format_id} onImported={result => { if (result.state) applyState(result.state); setTeamsTab('mine'); }} /> :
        <div className="team-grid">
          {state.teams.map(team => <article key={team.name} className={'team-card' + (team.name === selected?.name ? ' selected' : '')}>
            <header><strong title={team.name}>{team.name.replace(/\.txt$/i, '')}</strong><span>{formatName(state.format_choices, team.format_id ?? '')} • {team.profile?.label ?? '-'}{(() => { const r = state.team_records?.[team.name]; return r ? ` • ${r.wins}W ${r.losses}L` : ''; })()}</span></header>
            <div className="team-card-icons">{team.members.map((member, i) => <PokemonIcon key={member.species + i} species={member.species} />)}</div>
            {team.error && <p className="field-error">{team.error}</p>}
            <footer>
              <button className="small-button" disabled={disabled || team.name === selected?.name} onClick={() => selectTeam(team)}>{team.name === selected?.name ? 'Selected' : 'Use'}</button>
              <IconButton label={`Edit ${team.name}`} disabled={disabled} onClick={() => setEditor(team)}><Pencil size={15} /></IconButton>
              <IconButton label={`Duplicate ${team.name}`} disabled={disabled} onClick={() => copyTeam(team)}><Copy size={15} /></IconButton>
              <IconButton label={`Delete ${team.name}`} disabled={disabled} onClick={() => setDeleting(team)}><Trash2 size={15} /></IconButton>
            </footer>
          </article>)}
        </div>}
      </section>}
      {view === 'settings' && <section className="card settings-view" aria-label="Settings">
        <header className="card-header"><h2>SETTINGS</h2></header>
        <div className="settings-grid">
          <div className="setting"><div><strong>Account</strong><span>{state.config.username ? `${state.config.username} • ${state.config.mode === 'ladder' ? 'Ladder' : state.config.mode === 'challenge' ? 'Challenge' : 'Accept challenges'}${state.account_saved ? ' • saved' : ''}` : 'No account set up'}</span></div><div className="tool-row"><button className="small-button" disabled={disabled} onClick={() => { startAfterAccount.current = false; setAccountOpen(true); }}>Edit</button>{state.config.username && <button className="small-button danger" disabled={disabled} onClick={() => { password.current = ''; void perform(signal => post('logout', {}, { signal })); }}>Log out</button>}</div></div>
          <AvatarSetting value={String(state.config.avatar ?? '')} disabled={pending} onSave={avatar => void saveConfig({ avatar })} />
          <div className="setting"><div><strong>Concurrent battles</strong><span>How many ladder battles run at once. More battles share the search CPU.</span></div>
            <div className="segmented" role="radiogroup" aria-label="Concurrent battles">{[1, 2, 3, 4, 5].map(count => <button key={count} role="radio" aria-checked={state.config.concurrent_battles === count} className={state.config.concurrent_battles === count ? 'on' : ''} disabled={disabled || (count > 1 && state.config.mode !== 'ladder')} onClick={() => void saveConfig({ concurrent_battles: count })}>{count}</button>)}</div></div>
          <div className="setting"><div><strong>CPU usage</strong><span>Low keeps the PC free; High thinks longer each turn. The simulator always runs at low priority.</span></div>
            <div className="segmented wide" role="radiogroup" aria-label="CPU usage">{([['low', 'Low'], ['medium', 'Medium'], ['high', 'High']] as const).map(([value, label]) => <button key={value} role="radio" aria-checked={state.config.cpu_level === value} className={state.config.cpu_level === value ? 'on' : ''} disabled={pending} onClick={() => void saveConfig({ cpu_level: value })}>{label}</button>)}</div></div>
          <div className="setting"><div><strong>Engine</strong><span>Who picks the moves. Foul Play: MCTS over the opponent's likely sets. Hybrid: Foul Play searches and the neural network breaks its near-ties. Applies from the next session.</span></div>
            <div className="segmented wide" role="radiogroup" aria-label="Engine">{([['foulplay', 'Foul Play'], ['hybrid', 'Hybrid']] as const).map(([value, label]) => <button key={value} role="radio" aria-checked={(state.config.engine ?? 'hybrid') === value} className={(state.config.engine ?? 'hybrid') === value ? 'on' : ''} disabled={disabled} onClick={() => void saveConfig({ engine: value })}>{label}</button>)}</div></div>
          <div className="setting"><div><strong>Recover battle</strong><span>Reconnects to the saved battle and continues from the current turn.</span></div><button className="small-button" disabled={pending && !canStop} onClick={recover}><RotateCcw size={15} />Recover</button></div>
          <div className="setting"><div><strong>Learning</strong><span>Erases everything the bot learned from battles (matchups, seen moves, counters). History and teams stay.</span></div><button className="small-button danger" disabled={disabled} onClick={() => setResettingLearning(true)}><Trash2 size={15} />Erase</button></div>
          <div className="setting"><div><strong>History</strong><span>Clears the list and the counters. Learning, teams and replays are kept.</span></div><button className="small-button danger" disabled={disabled || !state.history.length} onClick={() => setClearingHistory(true)}><Trash2 size={15} />Clear</button></div>
        </div>
        <p className="modal-note">Tip: if the same account is logged in on your browser, Showdown opens the rooms there too. Use another account in the browser to avoid that.</p>
      </section>}
    </main>

    <footer className="system-footer">
      <span>CPU {state.metrics.cpu === null ? '-' : Math.round(state.metrics.cpu) + '%'}</span>
      <span>Latency {state.metrics.latency_ms === null ? '-' : Math.round(state.metrics.latency_ms) + ' ms'}</span>
    </footer>

    {accountOpen && <AccountDialog config={state.config} password={password.current} saved={Boolean(state.account_saved)} pending={pending} onSave={saveAccount} onClose={() => { if (!pending) { setAccountOpen(false); startAfterAccount.current = false; } }} />}
    {editor && <TeamDialog team={editor === 'new' ? undefined : editor} formatId={state.config.format_id} formats={state.format_choices} pending={pending} onSave={saveTeam} onClose={() => { if (!pending) setEditor(null); }} />}
    {deleting && <Modal title="Delete team" onClose={() => setDeleting(null)} footer={<><button className="modal-button" onClick={() => setDeleting(null)}>Cancel</button><button className="modal-button danger" disabled={pending} onClick={async () => { if (await perform(signal => post('team/delete', { name: deleting.name }, { signal }))) setDeleting(null); }}><Trash2 size={16} />Delete</button></>}><p>Delete "{deleting.name.replace(/\.txt$/i, '')}" from your teams?</p></Modal>}
    {battle && <BattleDialog record={battle} onClose={() => setBattle(null)} onReplay={openReplay} />}
    {clearingHistory && <Modal title="Clear history" onClose={() => { if (!pending) setClearingHistory(false); }} footer={<><button className="modal-button" disabled={pending} onClick={() => setClearingHistory(false)}>Cancel</button><button className="modal-button danger" disabled={pending} onClick={async () => { if (await perform(signal => post('history/reset', {}, { signal }))) { setClearingHistory(false); setBattle(null); } }}><Trash2 size={16} />Clear</button></>}><p>Clear every battle from the list and reset wins, losses and winrate? Teams and what the bot learned are kept.</p></Modal>}
    {resettingLearning && <Modal title="Erase learning" onClose={() => { if (!pending) setResettingLearning(false); }} footer={<><button className="modal-button" disabled={pending} onClick={() => setResettingLearning(false)}>Cancel</button><button className="modal-button danger" disabled={pending} onClick={async () => { if (await perform(signal => post('learning/reset', {}, { signal }))) setResettingLearning(false); }}><Trash2 size={16} />Erase</button></>}><p>Erase everything the bot learned? It goes back to deciding from the search and the team plan only. History, teams and replays are kept.</p></Modal>}
    {alert && <Modal title="Something went wrong" onClose={() => setAlert('')} footer={<button className="modal-button" onClick={() => setAlert('')}>Close</button>}><p role="alert">{alert}</p></Modal>}
  </div>;
}
