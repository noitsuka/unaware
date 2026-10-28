// Battle field: hazards and effects per side, weather and terrain (live card and history).
export interface FieldState { own?: Record<string, number>; foe?: Record<string, number>; weather?: string[]; fields?: string[] }

const SIDE: Record<string, [string, 'hazard' | 'boon']> = {
  STEALTH_ROCK: ['Stealth Rock', 'hazard'], SPIKES: ['Spikes', 'hazard'], TOXIC_SPIKES: ['Toxic Spikes', 'hazard'],
  STICKY_WEB: ['Sticky Web', 'hazard'], REFLECT: ['Reflect', 'boon'], LIGHT_SCREEN: ['Light Screen', 'boon'],
  AURORA_VEIL: ['Aurora Veil', 'boon'], TAILWIND: ['Tailwind', 'boon'], SAFEGUARD: ['Safeguard', 'boon'],
  MIST: ['Mist', 'boon'], LUCKY_CHANT: ['Lucky Chant', 'boon'],
};
const WEATHER: Record<string, string> = {
  SUNNYDAY: 'Sun', RAINDANCE: 'Rain', SANDSTORM: 'Sand', SNOWSCAPE: 'Snow', SNOW: 'Snow', HAIL: 'Hail',
  DESOLATELAND: 'Harsh sun', PRIMORDIALSEA: 'Heavy rain', DELTASTREAM: 'Strong winds',
};
const FIELDS: Record<string, string> = {
  ELECTRIC_TERRAIN: 'Electric Terrain', GRASSY_TERRAIN: 'Grassy Terrain', MISTY_TERRAIN: 'Misty Terrain',
  PSYCHIC_TERRAIN: 'Psychic Terrain', TRICK_ROOM: 'Trick Room', GRAVITY: 'Gravity', MAGIC_ROOM: 'Magic Room', WONDER_ROOM: 'Wonder Room',
};
const LAYERED = new Set(['SPIKES', 'TOXIC_SPIKES']);

function chips(side: Record<string, number> = {}) {
  return Object.entries(side).filter(([key]) => SIDE[key]).map(([key, value]) => {
    const [label, tone] = SIDE[key];
    return <span key={key} className={'field-chip ' + tone}>{label}{LAYERED.has(key) && value > 1 ? ` ×${value}` : ''}</span>;
  });
}

export function FieldSummary({ field, compact = false }: { field?: FieldState | null; compact?: boolean }) {
  if (!field) return null;
  const own = chips(field.own), foe = chips(field.foe);
  const global = [...(field.weather ?? []).map(w => WEATHER[w] ?? w), ...(field.fields ?? []).map(f => FIELDS[f] ?? f)];
  if (compact && !own.length && !foe.length && !global.length) return null;
  return <div className={'field-summary' + (compact ? ' compact' : '')}>
    <div><span className="field-side">Your side</span>{own.length ? own : <em>clear</em>}</div>
    <div><span className="field-side">Opponent</span>{foe.length ? foe : <em>clear</em>}</div>
    {global.length > 0 && <div><span className="field-side">Field</span>{global.map(name => <span key={name} className="field-chip global">{name}</span>)}</div>}
  </div>;
}

