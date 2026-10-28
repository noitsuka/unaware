import { useEffect, useMemo, useState } from 'react';
import { Download, LoaderCircle, Search } from 'lucide-react';
import { PokemonIcon } from './components';
import { post, request } from './api';
import type { ApiResult } from './types';

export interface LibraryTeam { name: string; source: string; format_id: string; archetype: string; species: string[]; setup_users: number; text: string }

const STYLE_LABELS: Record<string, string> = {
  hyper_offense: 'Hyper Offense', bulky_offense: 'Bulky Offense', balance: 'Balance',
  semi_stall: 'Semi-stall', stall: 'Stall', weather: 'Weather', auto: 'Auto',
};
const PAGE = 60;

const formatLabel = (formats: Record<string, string>, id: string) => (formats[id] || id).replace('[Gen 9] ', '');

/** Player-built teams (Smogon samples, tournament and ladder teams) to copy into "My teams". */
export function TeamLibrary({ disabled, formats, initialFormat, onImported }: { disabled: boolean; formats: Record<string, string>; initialFormat: string; onImported: (result: ApiResult) => void }) {
  const [teams, setTeams] = useState<LibraryTeam[] | null>(null);
  const [error, setError] = useState('');
  const [format, setFormat] = useState(initialFormat);
  const [style, setStyle] = useState('all');
  const [query, setQuery] = useState('');
  const [shown, setShown] = useState(PAGE);
  const [busy, setBusy] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    request<{ teams: LibraryTeam[] }>('library', {}, controller.signal, true)
      .then(data => setTeams(data.teams)).catch(problem => { if (!controller.signal.aborted) setError(String(problem.message ?? problem)); });
    return () => controller.abort();
  }, []);

  const available = useMemo(() => [...new Set((teams ?? []).map(team => team.format_id))], [teams]);
  const needle = query.trim().toLowerCase();
  const visible = useMemo(() => (teams ?? []).filter(team =>
    team.format_id === format && (style === 'all' || team.archetype === style)
    && (!needle || team.name.toLowerCase().includes(needle) || team.species.some(species => species.toLowerCase().includes(needle)))),
  [teams, format, style, needle]);
  useEffect(() => setShown(PAGE), [format, style, needle]);

  const importTeam = async (team: LibraryTeam) => {
    setBusy(team.name);
    try { onImported(await post('library/import', { name: team.name })); }
    catch (problem) { setError(problem instanceof Error ? problem.message : String(problem)); }
    finally { setBusy(''); }
  };

  if (error) return <p className="field-error">{error}</p>;
  if (!teams) return <p className="empty-note"><LoaderCircle size={16} className="animate-spin" /> Loading library...</p>;
  return <div className="library">
    <div className="library-filters">
      <div className="select-wrap library-format"><select aria-label="Library format" value={format} onChange={event => setFormat(event.target.value)}>
        {(available.includes(format) ? available : [format, ...available]).map(id => <option key={id} value={id}>{formatLabel(formats, id)}</option>)}
      </select></div>
      <div className="segmented wide">{['all', 'hyper_offense', 'bulky_offense', 'balance', 'semi_stall', 'stall', 'weather'].map(value =>
        <button key={value} className={style === value ? 'on' : ''} onClick={() => setStyle(value)}>{value === 'all' ? 'All' : STYLE_LABELS[value]}</button>)}</div>
      <label className="library-search"><Search size={14} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="Search a Pokémon" aria-label="Search a Pokémon" /></label>
      <span className="library-count">{visible.length} teams</span>
    </div>
    <div className="team-grid">
      {visible.slice(0, shown).map(team => <article key={team.format_id + team.source + team.name} className="team-card">
        <header><strong title={team.name}>{team.name}</strong><span>{STYLE_LABELS[team.archetype] ?? team.archetype}{team.setup_users ? ` • ${team.setup_users} setup` : ''}</span></header>
        <div className="team-card-icons">{team.species.map((species, i) => <PokemonIcon key={species + i} species={species} />)}</div>
        <footer><button className="small-button" disabled={disabled || busy === team.name} onClick={() => void importTeam(team)}><Download size={15} />Import</button></footer>
      </article>)}
      {visible.length > shown && <button className="small-button library-more" onClick={() => setShown(count => count + PAGE)}>Show more ({visible.length - shown} left)</button>}
      {!visible.length && <p className="empty-note">No teams match these filters.</p>}
    </div>
  </div>;
}
