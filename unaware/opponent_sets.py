"""Read coherent public opponent builds for inference, without team generation."""
from typing import Any

from .teams import TeamMember


def _choice(value: Any, variant: int = 0, default: Any = "") -> Any:
    if isinstance(value, list):
        return value[variant % len(value)] if value else default
    return default if value is None else value


def published_set(species: str, data: dict, variant: int = 0) -> TeamMember:
    """Slash alternatives stay inside one authored build, never across sets."""
    member = TeamMember(
        species=species, nickname=str(data.get("name", "")),
        item=str(_choice(data.get("item", ""), variant)),
        ability=str(_choice(data.get("ability", ""), variant)),
        nature=str(_choice(data.get("nature", "Serious"), variant)),
        moves=[str(_choice(move, variant)) for move in data.get("moves", [])],
        tera_type=str(_choice(data.get("teraType", data.get("teratypes", "")), variant)),
        level=int(data.get("level", 100)), gender=str(data.get("gender", "")),
        shiny=bool(data.get("shiny", False)),
    )
    for name in ("evs", "ivs"):
        stats = _choice(data.get(name, {}), variant, {})
        destination = getattr(member, name)
        if isinstance(stats, dict):
            destination.update({key: int(value) for key, value in stats.items() if key in destination})
    return member
