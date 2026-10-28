"""Opponent's position on the official ladder (top 500 of the format).

pokemonshowdown.com/ladder/<format>.json lists the top 500 with Elo and GXE.
The list is cached for 10 minutes per format; a failed download keeps the
previous list and the live card simply shows nothing.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request

URL = "https://pokemonshowdown.com/ladder/{}.json"
TTL = 600.0
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def to_id(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _toplist(format_id: str) -> dict:
    """userid -> {"rank", "elo", "gxe"} for the format's top 500 (cached)."""
    with _lock:
        cached = _cache.get(format_id)
        if cached and time.time() - cached[0] < TTL:
            return cached[1]
    try:
        request = urllib.request.Request(URL.format(format_id), headers={"User-Agent": "unaware"})
        with urllib.request.urlopen(request, timeout=10) as response:
            rows = json.loads(response.read()).get("toplist", [])
        table = {row["userid"]: {"rank": index, "elo": round(row.get("elo", 0)), "gxe": row.get("gxe")}
                 for index, row in enumerate(rows, 1)}
    except Exception:
        return cached[1] if cached else {}
    with _lock:
        _cache[format_id] = (time.time(), table)
    return table


def lookup(username: str, format_id: str) -> dict:
    """{"rank": 123, "elo": 1850, "gxe": 80.1} if in the top 500, {"rank": None} if not,
    {} when the ladder could not be read."""
    table = _toplist(format_id)
    if not table:
        return {}
    return table.get(to_id(username), {"rank": None})
