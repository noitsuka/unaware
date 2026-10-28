"""Read and write the human-readable Pokémon Showdown team format.

Validation here catches editing mistakes, not full tournament legality. The
Showdown server remains authoritative for bans, learnsets and special rules.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


FORMAT_CHOICES = {
    "gen9ou": "[Gen 9] OU",
    "gen9ubers": "[Gen 9] Ubers",
    "gen9uu": "[Gen 9] UU",
    "gen9ru": "[Gen 9] RU",
    "gen9nu": "[Gen 9] NU",
    "gen9pu": "[Gen 9] PU",
    "gen9lc": "[Gen 9] Little Cup",
    "gen9monotype": "[Gen 9] Monotype",
    "gen9anythinggoes": "[Gen 9] Anything Goes",
    "gen9randombattle": "[Gen 9] Random Battle",
}
FORMATS = FORMAT_CHOICES
SUPPORTED_FORMATS = tuple(FORMAT_CHOICES)
STAT_LABELS = {"hp": "HP", "atk": "Atk", "def": "Def", "spa": "SpA", "spd": "SpD", "spe": "Spe"}
STAT_ALIASES = {"hp": "hp", "atk": "atk", "def": "def", "spa": "spa", "spd": "spd", "spe": "spe", "spatk": "spa", "spdef": "spd", "speed": "spe"}
NATURES = frozenset("Adamant Bashful Bold Brave Calm Careful Docile Gentle Hardy Hasty Impish Jolly Lax Lonely Mild Modest Naive Naughty Quiet Quirky Rash Relaxed Sassy Serious Timid".split())
TYPES = frozenset("Normal Fire Water Electric Grass Ice Fighting Poison Ground Flying Psychic Bug Rock Ghost Dragon Dark Steel Fairy Stellar".split())


def to_id(value: str) -> str:
    value = value.replace("♀", "f").replace("♂", "m")
    value = unicodedata.normalize("NFKD", value)
    return re.sub(r"[^a-z0-9]", "", value.lower())


@dataclass
class TeamMember:
    species: str
    nickname: str = ""
    item: str = ""
    ability: str = ""
    nature: str = "Serious"
    evs: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAT_LABELS, 0))
    ivs: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAT_LABELS, 31))
    moves: list[str] = field(default_factory=list)
    tera_type: str = ""
    level: int = 100
    gender: str = ""
    shiny: bool = False


def _parse_stats(value: str, default: int, line_number: int) -> dict[str, int]:
    result = dict.fromkeys(STAT_LABELS, default)
    seen: set[str] = set()
    for chunk in value.split("/"):
        match = re.fullmatch(r"\s*(-?\d+)\s+([A-Za-z. ]+)\s*", chunk)
        if not match:
            raise ValueError(f"Line {line_number}: invalid EVs/IVs: {chunk.strip()!r}.")
        stat = STAT_ALIASES.get(to_id(match[2]))
        if stat is None or stat in seen:
            raise ValueError(f"Line {line_number}: unknown or repeated stat: {match[2].strip()}.")
        seen.add(stat)
        result[stat] = int(match[1])
    return result


def _header(line: str, line_number: int) -> TeamMember:
    identity, separator, item = line.partition(" @ ")
    if separator and not item.strip():
        raise ValueError(f"Line {line_number}: empty item after @.")
    gender = ""
    gender_match = re.search(r"\s+\(([MF])\)$", identity, re.IGNORECASE)
    if gender_match:
        gender = gender_match[1].upper()
        identity = identity[:gender_match.start()].strip()
    nickname = ""
    species_match = re.fullmatch(r"(.+?)\s+\(([^()]+)\)", identity)
    if species_match:
        nickname, identity = species_match[1], species_match[2]
    if not identity.strip() or identity.startswith(("-", "~")) or ":" in identity or "|" in identity:
        raise ValueError(f"Line {line_number}: invalid Pokémon header.")
    return TeamMember(species=identity.strip(), nickname=nickname, item=item.strip(), gender=gender)


def parse_team(text: str) -> list[TeamMember]:
    """Parse a single Showdown export; raise ValueError on unsupported syntax.

    Defaults match the import format: level 100, IVs 31, EVs 0. Blank lines
    separate members. A single ``=== [gen9ou] Team ===`` title is accepted.
    """
    members: list[TeamMember] = []
    current: TeamMember | None = None
    seen_fields: set[str] = set()
    title_seen = False
    for number, raw_line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            current = None
            seen_fields = set()
            continue
        if line.startswith("#"):
            continue
        if line.startswith("===") and line.endswith("==="):
            if title_seen or members:
                raise ValueError("Import one team at a time.")
            title_seen = True
            continue
        if current is None:
            current = _header(line, number)
            members.append(current)
            continue
        if line.startswith(("- ", "~ ")):
            move = line[2:].strip()
            if not move:
                raise ValueError(f"Line {number}: empty move.")
            current.moves.append(move)
            continue
        if line.lower().endswith(" nature"):
            key, value = "nature", line[:-7].strip()
        elif ":" in line:
            key, value = (part.strip() for part in line.split(":", 1))
            key = key.lower()
        else:
            raise ValueError(f"Line {number}: unrecognized line. Separate Pokémon with a blank line.")
        if key in seen_fields:
            raise ValueError(f"Line {number}: repeated field: {key}.")
        seen_fields.add(key)
        if key == "evs":
            current.evs = _parse_stats(value, 0, number)
        elif key == "ivs":
            current.ivs = _parse_stats(value, 31, number)
        elif key == "level":
            try:
                current.level = int(value)
            except ValueError as exc:
                raise ValueError(f"Line {number}: Level must be an integer.") from exc
        elif key == "shiny":
            if value.lower() not in {"yes", "no"}:
                raise ValueError(f"Line {number}: Shiny must be Yes or No.")
            current.shiny = value.lower() == "yes"
        elif key in {"ability", "nature", "tera type"}:
            setattr(current, "tera_type" if key == "tera type" else key, value)
        elif key == "gender":
            if value.upper() not in {"M", "F", "N"}:
                raise ValueError(f"Line {number}: Gender must be M, F or N.")
            current.gender = "" if value.upper() == "N" else value.upper()
        elif key in {"happiness", "pokeball", "hidden power", "dynamax level", "gigantamax"}:
            pass  # standard export fields with no effect on Gen 9 singles
        else:
            raise ValueError(f"Line {number}: unsupported field: {key}.")
    return members


def export_team(members: Iterable[TeamMember]) -> str:
    """Export all supported fields without dropping non-default EVs or IVs."""
    blocks = []
    for member in members:
        name = f"{member.nickname} ({member.species})" if member.nickname else member.species
        if member.gender:
            name += f" ({member.gender})"
        if member.item:
            name += f" @ {member.item}"
        lines = [name]
        if member.ability:
            lines.append(f"Ability: {member.ability}")
        if member.level != 100:
            lines.append(f"Level: {member.level}")
        if member.shiny:
            lines.append("Shiny: Yes")
        if member.tera_type:
            lines.append(f"Tera Type: {member.tera_type}")
        evs = " / ".join(f"{member.evs.get(stat, 0)} {label}" for stat, label in STAT_LABELS.items() if member.evs.get(stat, 0) != 0)
        if evs:
            lines.append(f"EVs: {evs}")
        if member.nature:
            lines.append(f"{member.nature} Nature")
        ivs = " / ".join(f"{member.ivs.get(stat, 31)} {label}" for stat, label in STAT_LABELS.items() if member.ivs.get(stat, 31) != 31)
        if ivs:
            lines.append(f"IVs: {ivs}")
        lines.extend(f"- {move}" for move in member.moves)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def validate_team(text: str, format_id: str) -> list[str]:
    """Return local editing errors. Tier legality must be checked by Showdown."""
    errors: list[str] = []
    if format_id not in FORMAT_CHOICES:
        errors.append(f"Unsupported format: {format_id}. Use Generation 9 singles.")
    if format_id == "gen9randombattle":
        return errors + (["Random Battle teams come from the server; leave the custom team empty."] if text.strip() else [])
    try:
        members = parse_team(text)
    except ValueError as exc:
        return errors + [str(exc)]
    if not 1 <= len(members) <= 6:
        errors.append("A team must have 1 to 6 Pokémon.")
    try:
        from poke_env.data import GenData
        dex = GenData.from_gen(9)
    except ImportError:
        dex = None
        errors.append("Install the dependencies (poke-env) to validate species and moves.")
    for index, member in enumerate(members, 1):
        prefix = f"Pokémon {index} ({member.species})"
        if not 1 <= member.level <= 100:
            errors.append(f"{prefix}: Level must be between 1 and 100.")
        if format_id == "gen9lc" and member.level != 5:
            errors.append(f"{prefix}: Little Cup requires Level: 5.")
        if any(not 0 <= value <= 252 for value in member.evs.values()) or sum(member.evs.values()) > 510:
            errors.append(f"{prefix}: EVs must be 0 to 252 per stat and 510 in total at most.")
        if any(not 0 <= value <= 31 for value in member.ivs.values()):
            errors.append(f"{prefix}: IVs must be between 0 and 31.")
        if not 1 <= len(member.moves) <= 4:
            errors.append(f"{prefix}: pick 1 to 4 moves.")
        if len({to_id(move) for move in member.moves}) != len(member.moves):
            errors.append(f"{prefix}: repeated moves.")
        if member.nature.title() not in NATURES:
            errors.append(f"{prefix}: unknown Nature: {member.nature}.")
        if member.tera_type and member.tera_type.title() not in TYPES:
            errors.append(f"{prefix}: unknown Tera Type: {member.tera_type}.")
        if dex is not None:
            species_data = dex.pokedex.get(to_id(member.species))
            if species_data is None:
                errors.append(f"{prefix}: species not found in the local Generation 9 data.")
            elif member.ability:
                abilities = {to_id(str(value)) for value in species_data.get("abilities", {}).values()}
                if abilities and to_id(member.ability) not in abilities:
                    errors.append(f"{prefix}: unknown ability for this species: {member.ability}.")
            for move in member.moves:
                if to_id(move) not in dex.moves:
                    errors.append(f"{prefix}: unknown move: {move}.")
    return errors
