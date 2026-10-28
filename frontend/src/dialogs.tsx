import { useEffect, useState } from 'react';
import { ChevronDown, FolderOpen, LoaderCircle, Plus, Save, Trash2 } from 'lucide-react';
import type { Archetype, BattleRecord, Config, Member, Team, TeamPreferences, TeamProfile } from './types';
import { Field, IconButton, Modal, PokemonIcon, Toggle } from './components';
import { battleId, blankMember, exportMembers, NATURES, parseExport, STATS, STAT_LABELS, TYPES } from './team';
import { readReport } from './api';
import { FieldSummary } from './field';
import { analyzeTeam, ARCHETYPES, detectedStyle, roleBrief } from './team-builder';

export function AccountDialog({ config, password, pending, saved = false, onSave, onClose }: { config: Config; password: string; pending: boolean; saved?: boolean; onSave: (config: Config, password: string) => Promise<void>; onClose: () => void }) {
  const [value, setValue] = useState({ ...config });
  const [secret, setSecret] = useState(password);
  const update = (key: string, next: unknown) => setValue(current => ({ ...current, [key]: next }));
  return <Modal title="Account" onClose={onClose} className="account-modal" footer={<><button className="modal-button" onClick={onClose}>Cancel</button><button className="modal-button primary" disabled={pending || !value.username.trim()} onClick={() => onSave(value, secret)}><Save size={16} />Save</button></>}>
    <div className="form-grid">
      <Field label="Login"><input autoComplete="username" value={value.username} maxLength={18} onChange={event => update('username', event.target.value)} placeholder="Your Showdown username" /></Field>
      <Field label="Password"><input type="password" autoComplete="current-password" value={secret} onChange={event => setSecret(event.target.value)} placeholder={saved ? "•••••••• (saved, leave empty to keep it)" : "Optional for unregistered names"} /></Field>
      <Field label="Battle mode"><select value={value.mode} onChange={event => update('mode', event.target.value)}><option value="ladder">Ladder</option><option value="challenge">Challenge a player</option><option value="accept">Accept challenges</option></select></Field>
      {value.mode !== 'ladder' && <Field label="Opponent"><input value={value.opponent} maxLength={18} onChange={event => update('opponent', event.target.value)} placeholder={value.mode === 'accept' ? 'Empty accepts anyone' : 'Player name'} /></Field>}
    </div>
    <p className="modal-note">The account is saved on this computer (encrypted by Windows) until you log out.</p>
  </Modal>;
}

function MemberDialog({ initial, onSave, onClose }: { initial: Member; onSave: (member: Member) => void; onClose: () => void }) {
  const [member, setMember] = useState(structuredClone(initial));
  const [error, setError] = useState('');
  const change = (key: keyof Member, value: unknown) => setMember(current => ({ ...current, [key]: value }));
  const apply = () => {
    if (!member.species.trim() || !member.moves.filter(Boolean).length) { setError('Enter the species and at least one move.'); return; }
    if (Object.values(member.evs).reduce((sum, value) => sum + value, 0) > 510) { setError('EVs can add up to 510 at most.'); return; }
    onSave({ ...member, moves: member.moves.filter(Boolean) });
  };
  return <Modal title={member.species || 'Add Pokémon'} onClose={onClose} className="member-modal" footer={<><button className="modal-button" onClick={onClose}>Cancel</button><button className="modal-button primary" onClick={apply}><Save size={16} />Apply</button></>}>
    <div className="form-grid two">
      {([['species', 'Species'], ['nickname', 'Nickname'], ['item', 'Item'], ['ability', 'Ability']] as const).map(([key, caption]) => <Field key={key} label={caption}><input value={member[key]} onChange={event => change(key, event.target.value)} /></Field>)}
      <Field label="Nature"><select value={member.nature} onChange={event => change('nature', event.target.value)}>{NATURES.map(nature => <option key={nature}>{nature}</option>)}</select></Field>
      <Field label="Tera Type"><select value={member.tera_type} onChange={event => change('tera_type', event.target.value)}><option value="">Auto</option>{TYPES.map(type => <option key={type}>{type}</option>)}</select></Field>
      {[0, 1, 2, 3].map(index => <Field key={index} label={`Move ${index + 1}`}><input value={member.moves[index] ?? ''} onChange={event => { const moves = [...member.moves]; moves[index] = event.target.value; change('moves', moves); }} /></Field>)}
    </div>
    <details className="advanced"><summary>EVs, IVs and more<ChevronDown size={16} /></summary>
      <div className="stats-grid"><span />{STAT_LABELS.map(stat => <span key={stat}>{stat}</span>)}
        {(['evs', 'ivs'] as const).map(key => <div className="stat-row" key={key}><span>{key.toUpperCase()}</span>{STATS.map(stat => <input key={stat} aria-label={`${key.toUpperCase()} ${stat}`} type="number" min={0} max={key === 'evs' ? 252 : 31} value={member[key][stat]} onChange={event => change(key, { ...member[key], [stat]: Math.min(key === 'evs' ? 252 : 31, Math.max(0, Number(event.target.value))) })} />)}</div>)}
      </div>
      <div className="form-grid two"><Field label="Level"><input type="number" min={1} max={100} value={member.level} onChange={event => change('level', Math.min(100, Math.max(1, Number(event.target.value))))} /></Field><Field label="Gender"><select value={member.gender} onChange={event => change('gender', event.target.value)}><option value="">Auto</option><option value="M">Male</option><option value="F">Female</option></select></Field><div className="switch-field"><span>Shiny</span><Toggle label="Shiny" checked={member.shiny} onChange={next => change('shiny', next)} /></div></div>
    </details>{error && <p className="field-error" role="alert">{error}</p>}
  </Modal>;
}

export function TeamDialog({ team, formatId, formats = {}, pending, onSave, onClose }: { team?: Team; formatId: string; formats?: Record<string, string>; pending: boolean; onSave: (name: string, text: string, originalName?: string, preferences?: TeamPreferences) => Promise<void>; onClose: () => void }) {
  const [name, setName] = useState(team?.name.replace(/\.txt$/i, '') ?? '');
  const [text, setText] = useState(team?.text ?? '');
  const [tier, setTier] = useState(team?.format_id ?? formatId);
  const [archetype, setArchetype] = useState<Archetype>(team?.archetype ?? 'auto');
  const [profile, setProfile] = useState<TeamProfile | null>(team?.profile ?? null);
  const [analyzing, setAnalyzing] = useState(false);
  const [editing, setEditing] = useState<{ index: number; member: Member } | null>(null);
  const [error, setError] = useState('');
  const busy = pending;
  useEffect(() => {
    setProfile(null);
    if (!text.trim()) { setAnalyzing(false); return; }
    setAnalyzing(true);
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      analyzeTeam(text, controller.signal).then(next => {
        if (!controller.signal.aborted) setProfile(next);
      }).catch(() => { /* Partial imports do not need an error popup. */ }).finally(() => {
        if (!controller.signal.aborted) setAnalyzing(false);
      });
    }, 300);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [text]);
  let members: Member[] = [];
  try { members = parseExport(text); } catch { /* The user may be in the middle of a line. */ }
  const edit = (index: number) => {
    try {
      const parsed = parseExport(text);
      if (index < 0 && parsed.length >= 6) { setError('The team already has six Pokémon.'); return; }
      const member = index < 0 ? blankMember() : parsed[index];
      if (index < 0 && tier === 'gen9lc') member.level = 5;
      setEditing({ index, member });
      setError('');
    } catch (problem) { setError(String(problem instanceof Error ? problem.message : problem)); }
  };
  const importFile = async () => {
    const picker = document.createElement('input'); picker.type = 'file'; picker.accept = '.txt,text/plain';
    picker.onchange = async () => { const file = picker.files?.[0]; if (file) { setText(await file.text()); if (!name) setName(file.name.replace(/\.txt$/i, '')); } };
    picker.click();
  };
  if (editing) return <MemberDialog initial={editing.member} onClose={() => setEditing(null)} onSave={member => {
    const next = parseExport(text); if (editing.index < 0) next.push(member); else next[editing.index] = member;
    setText(exportMembers(next)); setEditing(null);
  }} />;
  return <Modal title={team ? 'Edit team' : 'New team'} onClose={onClose} className="team-modal" footer={<><button className="modal-button" disabled={busy} onClick={importFile}><FolderOpen size={16} />Import file</button><span className="flex-1" /><button className="modal-button" onClick={onClose}>Cancel</button><button className="modal-button primary" disabled={busy || !name.trim() || !text.trim()} onClick={() => onSave(name.trim(), text, team?.name, { format_id: tier, archetype })}><Save size={16} />Save team</button></>}>
    <Field label="Team name"><input value={name} onChange={event => setName(event.target.value)} placeholder="e.g. Rain Dance [OU]" /></Field>
    <div className="builder-toolbar">
      <Field label="Format"><select value={tier} disabled={busy} onChange={event => setTier(event.target.value)}>{Object.entries(Object.keys(formats).length ? formats : { [tier]: tier.replace(/^gen9/, 'Gen 9 ').toUpperCase() }).map(([key, label]) => <option key={key} value={key}>{label.replace('[Gen 9]', 'Gen 9')}</option>)}</select></Field>
      <Field label="Play style"><select value={archetype} disabled={busy} onChange={event => setArchetype(event.target.value as Archetype)}>{Object.entries(ARCHETYPES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>
    </div>
    <div className="team-profile" aria-live="polite"><span>{analyzing ? 'Analysing style...' : profile ? `Detected: ${detectedStyle(profile)}` : text.trim() ? 'Style not identified' : 'Add your Pokémon or import a team.'}</span>{profile && roleBrief(profile.roles) && <span className="role-brief">{roleBrief(profile.roles)}</span>}</div>
    <div className="team-editor"><Field label="Pokémon Showdown export"><textarea value={text} disabled={busy} onChange={event => setText(event.target.value)} spellCheck={false} placeholder="Paste a Showdown export here or add your Pokémon." /></Field>
      <div className="editor-roster"><span className="field-label">Pokémon · {members.length}/6</span>{members.map((member, index) => <div className="editor-mon" key={index}><button disabled={busy} onClick={() => edit(index)}><PokemonIcon species={member.species} /><span>{member.species}</span></button><IconButton label={`Remove ${member.species}`} disabled={busy} onClick={() => setText(exportMembers(members.filter((_, i) => i !== index)))}><Trash2 size={14} /></IconButton></div>)}<button className="modal-button" disabled={busy || members.length >= 6} onClick={() => edit(-1)}><Plus size={16} />Add Pokémon</button></div>
    </div>{error && <p className="field-error" role="alert">{error}</p>}
  </Modal>;
}

export function BattleDialog({ record, onClose, onReplay }: { record: BattleRecord; onClose: () => void; onReplay: (record: BattleRecord) => void }) {
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    readReport(battleId(record), controller.signal).then(value => {
      if (!controller.signal.aborted) setReport(value);
    }).catch(problem => { if (!controller.signal.aborted) setError(problem.message); });
    return () => controller.abort();
  }, [record]);
  const findings = Array.isArray(report?.findings) ? report.findings as Record<string, unknown>[] : [];
  const decisions = Array.isArray(report?.decisions) ? report.decisions as Record<string, unknown>[] : [];
  const fieldOf = (decision?: Record<string, unknown>) => {
    const before = (decision?.before ?? {}) as Record<string, unknown>;
    return { own: before.own_hazards as Record<string, number>, foe: before.foe_hazards as Record<string, number>,
             weather: before.weather as string[], fields: before.fields as string[] };
  };
  return <Modal title={`Battle · ${record.opponent || 'Opponent'}`} onClose={onClose} className="battle-modal" footer={<><button className="modal-button" disabled={!record.replay_path} onClick={() => onReplay(record)}><FolderOpen size={16} />Open replay</button><span className="flex-1" /><button className="modal-button" onClick={onClose}>Close</button></>}>
    {!report && !error && <div className="report-loading"><LoaderCircle size={22} className="animate-spin" />Loading analysis...</div>}
    {error && <p className="field-error" role="alert">{error}</p>}
    {report && <><p className="report-summary">{String(report.summary || record.summary || '')}</p>
      {decisions.length > 0 && <section className="report-field"><h3>Field at the end</h3><FieldSummary field={fieldOf(decisions[decisions.length - 1])} /></section>}<div className="findings">{findings.map((finding, index) => <article key={index}><h3>{finding.turn ? `Turn ${finding.turn} · ` : ''}{String(finding.title || 'Observation')}</h3><p>{String(finding.detail || '')}</p>{finding.hypothesis ? <p className="text-muted">{String(finding.hypothesis)}</p> : null}</article>)}</div>
      {decisions.length > 0 && <details className="advanced"><summary>Decisions<ChevronDown size={16} /></summary><div className="decision-list">{decisions.map((decision, index) => <div key={index}><span>Turn {String(decision.turn ?? index + 1)}</span><strong>{String((decision.chosen as Record<string, unknown>)?.label ?? decision.chosen ?? decision.action_key ?? '')}</strong><FieldSummary field={fieldOf(decision)} compact /></div>)}</div></details>}
    </>}
  </Modal>;
}
