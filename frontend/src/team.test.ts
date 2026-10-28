import { describe, expect, it } from 'vitest';
import { battleId, exportMembers, id, parseExport } from './team';

const exportText = `Ace (Dragonite) (M) @ Heavy-Duty Boots
Ability: Multiscale
Level: 98
Shiny: Yes
Tera Type: Normal
EVs: 252 Atk / 4 SpD / 252 Spe
Adamant Nature
IVs: 0 SpA
- Dragon Dance
- Extreme Speed
- Earthquake
- Ice Spinner

Nidoran-F (F) @ Eviolite
Ability: Hustle
Level: 5
EVs: 252 HP / 252 Atk / 4 SpD
Adamant Nature
- Poison Jab
`;

describe('Showdown editor preservation', () => {
  it('preserves nickname, gender, shiny, level, zero IVs and every EV through editing', () => {
    const members = parseExport(exportText);
    expect(parseExport(exportMembers(members))).toEqual(members);
    expect(members[0].ivs.spa).toBe(0);
    expect(members[0].evs.atk).toBe(252);
    expect(members[0].nickname).toBe('Ace');
    expect(members[0].gender).toBe('M');
    expect(members[0].level).toBe(98);
    expect(members[0].shiny).toBe(true);
  });
  it('accepts Windows line endings and UTF-8 BOM without losing the first species', () => {
    expect(parseExport('\ufeff' + exportText.replaceAll('\n', '\r\n'))[0].species).toBe('Dragonite');
  });
  it('keeps default IVs at 31 and unspecified EVs at zero', () => {
    const member = parseExport('Pikachu\n- Thunderbolt')[0];
    expect(member.ivs).toEqual({ hp: 31, atk: 31, def: 31, spa: 31, spd: 31, spe: 31 });
    expect(Object.values(member.evs).every(value => value === 0)).toBe(true);
  });
  it('does not silently discard unrecognized attributes during guided editing', () => {
    expect(() => parseExport('Pikachu\nUnknown: content\n- Thunderbolt')).toThrow('Unrecognized field');
  });
  it('normalizes symbols and alternate forms to their actual sprite identifiers', () => {
    expect(id('Nidoran♀')).toBe('nidoranf');
    expect(id('Toxtricity-Low-Key')).toBe('toxtricitylowkey');
  });
  it('identifies history by server and battle instead of accidentally merging servers', () => {
    expect(battleId({ battle_id: 'battle-one', server_namespace: 'localhost:8000' })).toBe('localhost:8000::battle-one');
    expect(battleId({ battle_id: 'battle-one', storage_id: 'sim3.psim.us::battle-one' })).toBe('sim3.psim.us::battle-one');
  });
});
