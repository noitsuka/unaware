import type { Member, Stats } from './types';

export const STATS = ['hp', 'atk', 'def', 'spa', 'spd', 'spe'] as const;
export const STAT_LABELS = ['HP', 'Atk', 'Def', 'SpA', 'SpD', 'Spe'];
export const NATURES = 'Adamant Bashful Bold Brave Calm Careful Docile Gentle Hardy Hasty Impish Jolly Lax Lonely Mild Modest Naive Naughty Quiet Quirky Rash Relaxed Sassy Serious Timid'.split(' ');
export const TYPES = 'Normal Fire Water Electric Grass Ice Fighting Poison Ground Flying Psychic Bug Rock Ghost Dragon Dark Steel Fairy Stellar'.split(' ');
export const id = (text: string) => text.replaceAll('♀', 'f').replaceAll('♂', 'm').normalize('NFKD').toLowerCase().replace(/[^a-z0-9]/g, '');
const stats = (value: number): Stats => ({ hp: value, atk: value, def: value, spa: value, spd: value, spe: value });
export const blankMember = (): Member => ({ species: '', nickname: '', item: '', ability: '', nature: 'Serious', evs: stats(0), ivs: stats(31), moves: [], tera_type: '', level: 100, gender: '', shiny: false });

function parseStats(value: string, original: Stats): Stats {
  const result = { ...original };
  for (const chunk of value.split('/')) {
    const match = chunk.trim().match(/^(\d+)\s+(\w+)$/);
    if (!match || !STATS.includes(match[2].toLowerCase() as typeof STATS[number])) throw new Error('Invalid EVs or IVs.');
    result[match[2].toLowerCase() as typeof STATS[number]] = Number(match[1]);
  }
  return result;
}

export function parseExport(text: string): Member[] {
  const members: Member[] = [];
  let member: Member | null = null;
  for (const raw of text.replace(/^\uFEFF/, '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) { member = null; continue; }
    if (line.startsWith('#') || /^===.*===$/.test(line)) continue;
    if (!member) {
      member = blankMember();
      let [identity, item] = line.split(' @ ');
      member.item = item ?? '';
      const gender = identity.match(/\s+\(([MF])\)$/);
      if (gender) { member.gender = gender[1]; identity = identity.slice(0, gender.index).trim(); }
      const nickname = identity.match(/^(.+?)\s+\(([^()]+)\)$/);
      member.species = nickname ? nickname[2] : identity;
      member.nickname = nickname ? nickname[1] : '';
      if (!member.species || /[:|]/.test(member.species) || member.species.startsWith('-')) throw new Error('Invalid Pokémon header.');
      members.push(member);
    } else if (/^[-~]\s/.test(line)) member.moves.push(line.slice(2).trim());
    else if (line.endsWith(' Nature')) member.nature = line.slice(0, -7);
    else {
      const colon = line.indexOf(':');
      if (colon === -1) throw new Error('Separate each Pokémon with a blank line.');
      const key = line.slice(0, colon).toLowerCase(), value = line.slice(colon + 1).trim();
      if (key === 'ability') member.ability = value;
      else if (key === 'tera type') member.tera_type = value;
      else if (key === 'evs') member.evs = parseStats(value, member.evs);
      else if (key === 'ivs') member.ivs = parseStats(value, member.ivs);
      else if (key === 'level') member.level = Number(value);
      else if (key === 'shiny') member.shiny = value.toLowerCase() === 'yes';
      else if (key === 'gender') member.gender = /^[MF]$/i.test(value) ? value.toUpperCase() : '';
      // Standard export fields that do not affect Gen 9 singles.
      else if (['happiness', 'pokeball', 'hidden power', 'dynamax level', 'gigantamax'].includes(key)) continue;
      else throw new Error('Unrecognized field: ' + key);
    }
  }
  return members;
}

export function exportMembers(members: Member[]): string {
  return members.map(member => {
    const lines = [
      (member.nickname ? `${member.nickname} (${member.species})` : member.species) +
      (member.gender ? ` (${member.gender})` : '') + (member.item ? ` @ ${member.item}` : ''),
    ];
    if (member.ability) lines.push('Ability: ' + member.ability);
    if (member.level !== 100) lines.push('Level: ' + member.level);
    if (member.shiny) lines.push('Shiny: Yes');
    if (member.tera_type) lines.push('Tera Type: ' + member.tera_type);
    const evs = STATS.map((stat, index) => member.evs[stat] ? `${member.evs[stat]} ${STAT_LABELS[index]}` : '').filter(Boolean);
    if (evs.length) lines.push('EVs: ' + evs.join(' / '));
    lines.push(member.nature + ' Nature');
    const ivs = STATS.map((stat, index) => member.ivs[stat] !== 31 ? `${member.ivs[stat]} ${STAT_LABELS[index]}` : '').filter(Boolean);
    if (ivs.length) lines.push('IVs: ' + ivs.join(' / '));
    lines.push(...member.moves.filter(Boolean).map(move => '- ' + move));
    return lines.join('\n');
  }).join('\n\n') + '\n';
}

export function battleId(record: { storage_id?: string; battle_id: string; server_namespace?: string }): string {
  return record.storage_id || (record.server_namespace ? record.server_namespace + '::' : '') + record.battle_id;
}
