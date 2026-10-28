"""Restore own public request data omitted by poke-env without resetting battle state."""
from __future__ import annotations

from poke_env.battle import PokemonType
from poke_env.data import to_id_str, GenData

STATS = ("hp", "atk", "def", "spa", "spd", "spe")


def hydrate_request(battle, members=()):
    request = getattr(battle, "_last_request", None) or {}
    records = request.get("side", {}).get("pokemon", [])
    roster = list(getattr(battle, "team", {}).items())
    active = getattr(battle, "active_pokemon", None)
    for identifier, mon in roster:
        record = next((r for r in records if r.get("ident") == identifier), {})
        nickname = to_id_str(identifier.partition(":")[2])
        member = next((m for m in members if nickname and nickname == to_id_str(m.nickname or m.species)), None)
        if member is None:
            matching = [m for m in members if to_id_str(m.species) == mon.species]
            member = matching[0] if len(matching) == 1 else None
        if member is not None:
            # Items, abilities, moves and server stats can change during battle.
            # Only set immutable original spread data from our own exported team.
            mon._evs = [member.evs[k] for k in STATS]
            mon._ivs = [member.ivs[k] for k in STATS]
            mon._nature = to_id_str(member.nature)
        tera = record.get("teraType")
        if not tera and mon is active:
            entries = request.get("active") or []
            value = entries[0].get("canTerastallize") if entries else None
            tera = value if isinstance(value, str) else None
        if not tera and member is not None:
            tera = member.tera_type
        if tera and not mon.is_terastallized:
            try:
                mon._terastallized_type = PokemonType.from_name(tera)
            except (KeyError, ValueError):
                pass


def hydrate_public_event(battle, message):
    """Retain explicitly revealed item sources the protocol parser omits.

    A [from] item source belongs to [of], when present, rather than the Pokémon
    receiving damage (e.g. Rocky Helmet). Status alone never reveals an item.
    """
    if battle is None or len(message) < 4:
        return
    roster = {**getattr(battle, "team", {}), **getattr(battle, "_opponent_team", getattr(battle, "opponent_team", {}))}
    if message[1] in {"detailschange", "-formechange"}:
        identifier = message[2][:2] + ": " + message[2].partition(":")[2].strip()
        mon = roster.get(identifier)
        species = to_id_str(message[3].partition(",")[0])
        if mon is not None and species in GenData.from_gen(getattr(battle, "gen", 9)).pokedex:
            # poke-env updates stats/types but intentionally keeps the old
            # species ID. The simulator needs the publicly revealed forme to
            # avoid rebuilding a broken Disguise/Ice Face as intact again.
            mon._species = species
        return
    if message[1] not in {"-status", "-damage", "-heal", "-activate"}:
        return
    source = next((part.removeprefix("[from] item:").strip() for part in message[3:]
                   if part.startswith("[from] item:")), "")
    if not source:
        return
    holder = next((part.removeprefix("[of]").strip() for part in message[3:] if part.startswith("[of]")), message[2])
    identifier = holder[:2] + ": " + holder.partition(":")[2].strip()
    mon = roster.get(identifier)
    if mon is not None:
        mon._item = to_id_str(source)
