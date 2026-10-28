"""Opponent scouting as a prior for Foul Play's set sampling (3.14).

Foul Play guesses the opponent's sets from public data only: sets scraped from
many players' replays and Smogon usage. Our scouting already downloads this
player's own recent replays. In the 51 ladder games of 14/10, 22 opponents
brought exactly the team scouting had seen, and 41% of the moves used against
us had been seen in their replays.

The prior is soft and only for teams scouting recognises (4+ of the 6 species
seen together in one of the player's games):
- the full sets Foul Play samples from are re-weighted so the ones agreeing
  with what this player showed (moves, item, Tera) make up ~75% of the draws;
- the moves this player showed are added as a moveset with high weight.
Foul Play still discards any set the battle contradicts, so a player who
changed sets falls back to the public data as before.
"""
from __future__ import annotations

import math
import re
from collections import Counter

MIN_TEAM_OVERLAP = 4
SHARE = .75


def to_id(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def base_id(species: str) -> str:
    try:
        from .strategy import base_species_id
        return base_species_id(species)
    except Exception:
        return to_id(species)


def scouted_sets(games: list[dict], preview_species, exclude_ids=()) -> dict[str, dict]:
    """Per base species: Counters of moves/items/tera/abilities this player
    revealed in games with (nearly) the same team as the current preview."""
    preview = {base_id(s) for s in preview_species}
    out: dict[str, dict] = {}
    for game in games or []:
        if str(game.get("id", "")) in exclude_ids:
            continue
        team = game.get("team") or {}
        species = {base_id(s) for s in team.get("species", [])}
        if len(species & preview) < MIN_TEAM_OVERLAP:
            continue
        for shown in team.get("revealed", []):
            key = base_id(shown.get("species", ""))
            if key not in preview:
                continue
            entry = out.setdefault(key, {"moves": Counter(), "items": Counter(), "tera": Counter(),
                                         "abilities": Counter(), "games": 0})
            entry["games"] += 1
            entry["moves"].update(to_id(m) for m in shown.get("moves", []))
            entry["items"].update(to_id(i) for i in shown.get("items", []) if i)
            entry["tera"].update(to_id(t) for t in shown.get("tera_types", []) if t)
            entry["abilities"].update(to_id(a) for a in shown.get("abilities", []) if a)
    return out


def _agrees(predicted, shown: dict) -> bool:
    moves = {to_id(m) for m in predicted.pkmn_moveset.moves}
    seen = [m for m, _ in shown["moves"].most_common(4)]
    if seen and not all(m in moves for m in seen):
        return False
    item = shown["items"].most_common(1)
    if item and to_id(predicted.pkmn_set.item) != item[0][0]:
        return False
    tera = shown["tera"].most_common(1)
    if tera and to_id(predicted.pkmn_set.tera_type) != tera[0][0]:
        return False
    return True


def apply_prior(team_datasets, prior: dict[str, dict]) -> dict[str, int]:
    """Re-weight Foul Play's TeamDatasets in place. Returns, per dataset key,
    how many full sets agreed with the scouting (for the log)."""
    from fp.data.sets import PokemonMoveset
    applied = {}
    for name in list(team_datasets.pkmn_sets):
        shown = prior.get(base_id(name))
        if not shown:
            continue
        sets = team_datasets.pkmn_sets[name]
        agree = [s for s in sets if _agrees(s, shown)]
        others = [s for s in sets if not _agrees(s, shown)]
        if agree and others:
            # Foul Play draws uniformly from this list: repeat the agreeing sets.
            copies = max(1, math.ceil(SHARE / (1 - SHARE) * len(others) / len(agree)))
            team_datasets.pkmn_sets[name] = agree * copies + others
        applied[name] = len(agree)
        moves = tuple(m for m, _ in shown["moves"].most_common(4))
        if len(moves) >= 2:
            existing = team_datasets.raw_pkmn_moves.setdefault(name, [])
            total = sum(m.count for m in existing) or 1
            # Weight ~3x everything public: still overruled by any contradiction.
            existing.append(PokemonMoveset(moves=moves, count=3 * total))
    return applied
