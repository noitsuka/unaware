"""Download published Smogon monthly usage distributions with a local cache.

These are population statistics, not the opponent's actual set. Discovery uses
directory indexes rather than assuming that the current month is published.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .teams import SUPPORTED_FORMATS, to_id

BASE_URL = "https://www.smogon.com/stats/"
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024


def rating_bucket(rating: float | None) -> int:
    """Elo selects a population prior; Smogon cutoffs are not literal Elo.

    The available published cutoff is resolved by update(), including tiers
    that publish different cutoffs. Unknown Elo starts with the broad sample.
    """
    if rating is None or rating < 1400:
        return 0
    if rating < 1625:
        return 1500
    return 1695 if rating < 1775 else 1825


def blend_preferences(local: dict, research: dict | None = None) -> dict:
    """Blend incomplete public observations conservatively, below usage data."""
    researched = (research or {}).get("species", {})
    output = {}
    for species in local.keys() | researched.keys():
        own, public = local.get(species, {}), researched.get(species, {})
        left = min(.25, max(0, float(own.get("confidence", 0))))
        right = min(.15, max(0, float(public.get("confidence", 0))))
        total = left + right
        moves = own.get("moves", {}).keys() | public.get("moves", {}).keys()
        output[species] = {"confidence": min(.25, total), "battles": own.get("battles", 0),
                           "research_battles": public.get("battles", 0),
                           "moves": {move: (left * own.get("moves", {}).get(move, 0) +
                                             right * public.get("moves", {}).get(move, 0)) / total
                                     for move in moves} if total else {}}
    return output


class MetaUpdateError(RuntimeError):
    """The remote source could not provide a usable, format-specific snapshot."""


@dataclass(frozen=True)
class MetaSnapshot:
    format_id: str
    month: str
    rating: int
    source_url: str
    data: dict[str, dict[str, Any]]
    downloaded_at: str = ""
    warnings: tuple[str, ...] = ()
    info: dict[str, Any] = field(default_factory=dict)

    def species(self, name: str) -> dict[str, Any]:
        """Return the original chaos species fields, or an empty distribution."""
        return self.data.get(to_id(name), {})


class MetaStore:
    def __init__(self, cache_dir: Path, timeout: float = 20.0):
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.last_error: str | None = None
        self._deadline: float | None = None

    @staticmethod
    def _check_format(format_id: str) -> None:
        if format_id not in SUPPORTED_FORMATS:
            raise ValueError(f"Unsupported format: {format_id}.")

    def _fetch(self, url: str) -> str:
        remaining = self._deadline - time.monotonic() if self._deadline else self.timeout
        if remaining <= 0:
            raise MetaUpdateError("The meta update timed out.")
        request = urllib.request.Request(url, headers={"User-Agent": "Unaware/1.0 (Smogon usage statistics)"})
        with urllib.request.urlopen(request, timeout=min(self.timeout, remaining)) as response:
            body = response.read(MAX_DOWNLOAD_BYTES + 1)
        if len(body) > MAX_DOWNLOAD_BYTES:
            raise MetaUpdateError("Meta file larger than the 40 MiB limit.")
        return body.decode("utf-8")

    def load(self, format_id: str, rating: int | None = None) -> MetaSnapshot | None:
        """Read cache only. Corrupt/missing files never masquerade as fresh data."""
        self._check_format(format_id)
        self.last_error = None
        path = self.cache_dir / f"{format_id}.json"
        requested_rating, selected_profile = rating, False
        if rating is not None:
            profile = self.cache_dir / f"{format_id}-{int(rating)}.json"
            if profile.is_file():
                path = profile
                selected_profile = True
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("schema") != 1 or raw.get("format_id") != format_id:
                raise ValueError("incompatible cache format")
            self._validate_data(raw.get("data"))
            if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(raw.get("month", ""))):
                raise ValueError("invalid month")
            rating = int(raw["rating"])
            expected_url = f"{BASE_URL}{raw['month']}/chaos/{format_id}-{rating}.json"
            if raw.get("source_url") != expected_url:
                raise ValueError("invalid source URL")
            return MetaSnapshot(
                format_id=format_id, month=raw["month"], rating=rating,
                source_url=expected_url, data={to_id(name): value for name, value in raw["data"].items()},
                downloaded_at=str(raw.get("downloaded_at", "")),
                warnings=tuple(str(warning) for warning in raw.get("warnings", [])) +
                    ((f"No cache yet for the {requested_rating} cutoff; using the available {rating} cutoff for now.",)
                     if requested_rating is not None and not selected_profile and requested_rating != rating else ()) +
                    ("Local cache: newer stats have not been checked yet this session.",),
                info=raw.get("info", {}),
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            self.last_error = f"Invalid meta cache: {exc}"
            return None

    @staticmethod
    def _validate_data(data: Any) -> None:
        if not isinstance(data, dict) or not data:
            raise ValueError("missing or empty distributions")
        if any(not isinstance(name, str) or not isinstance(value, dict) for name, value in data.items()):
            raise ValueError("invalid species distributions")

    def update(self, format_id: str, rating: int = 1695, max_duration: float | None = None) -> MetaSnapshot:
        """Fetch the newest published month containing this format.

        If a requested rating bucket is absent, choose the closest published
        bucket (lower bucket wins ties) and explicitly record the fallback.
        Up to six recent published months are considered for absent formats.
        A failed refresh leaves any existing cache intact.
        """
        self._check_format(format_id)
        self.last_error = None
        if not isinstance(rating, int) or isinstance(rating, bool) or rating < 0:
            raise ValueError("Rating must be a non-negative integer.")
        if format_id == "gen9randombattle":
            raise MetaUpdateError("Random Battle does not use Smogon's built-team stats. The bot uses revealed data and types.")
        self._deadline = time.monotonic() + max_duration if max_duration else None
        try:
            root_html = self._fetch(BASE_URL)
            months = sorted(set(re.findall(r'href=[\"\'](\d{4}-(?:0[1-9]|1[0-2]))/[\"\']', root_html, re.IGNORECASE)), reverse=True)
            if not months:
                raise MetaUpdateError("Could not find published months in the Smogon index.")
            failures = []
            for month in months[:6]:
                directory_url = f"{BASE_URL}{month}/chaos/"
                try:
                    directory_html = self._fetch(directory_url)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        failures.append(f"{month}: chaos directory unavailable")
                        continue
                    raise
                ratings = {int(value) for value in re.findall(r'href=[\"\']' + re.escape(format_id) + r'-(\d+)\.json[\"\']', directory_html, re.IGNORECASE)}
                if not ratings:
                    failures.append(f"{month}: no {format_id}")
                    continue
                actual_rating = min(ratings, key=lambda candidate: (abs(candidate - rating), candidate))
                source_url = f"{directory_url}{format_id}-{actual_rating}.json"
                payload = json.loads(self._fetch(source_url))
                if not isinstance(payload, dict):
                    raise ValueError("invalid JSON response")
                self._validate_data(payload.get("data"))
                warnings = ["Aggregate frequencies do not reveal the opponent's actual set; changes after publication may be missing."]
                if actual_rating != rating:
                    warnings.append(f"Rating {rating} unavailable for {format_id}; using the published {actual_rating} bucket.")
                if month != months[0]:
                    warnings.append(f"The latest published month is {months[0]}, but {format_id} was only found in {month}.")
                normalized = {to_id(name): value for name, value in payload["data"].items()}
                snapshot = MetaSnapshot(
                    format_id=format_id, month=month, rating=actual_rating,
                    source_url=source_url, data=normalized,
                    downloaded_at=datetime.now(timezone.utc).isoformat(),
                    warnings=tuple(warnings), info=payload.get("info", {}),
                )
                self._save(snapshot, requested_rating=rating)
                return snapshot
            raise MetaUpdateError(f"No published {format_id} stats in the last six available months. " + "; ".join(failures))
        except (OSError, ValueError, UnicodeError, MetaUpdateError) as exc:
            self.last_error = str(exc)
            if isinstance(exc, MetaUpdateError):
                raise
            raise MetaUpdateError(f"Meta update failed: {exc}. The previous cache was kept.") from exc
        finally:
            self._deadline = None

    def _save(self, snapshot: MetaSnapshot, requested_rating: int | None = None) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        destination = self.cache_dir / f"{snapshot.format_id}.json"
        record = {
            "schema": 1, "format_id": snapshot.format_id, "month": snapshot.month,
            "rating": snapshot.rating, "source_url": snapshot.source_url,
            "downloaded_at": snapshot.downloaded_at, "warnings": list(snapshot.warnings),
            "data": snapshot.data, "info": snapshot.info,
        }
        temporary: Path | None = None
        try:
            # Migrate a legacy single-file cache before replacing its only
            # copy with another population. Corrupt legacy caches are ignored.
            previous = self.load(snapshot.format_id)
            if previous and previous.rating != snapshot.rating:
                profile = self.cache_dir / f"{snapshot.format_id}-{previous.rating}.json"
                if not profile.exists():
                    with tempfile.NamedTemporaryFile(mode="wb", dir=self.cache_dir, prefix="meta-", suffix=".tmp", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(destination.read_bytes())
                    temporary.replace(profile)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir, prefix="meta-", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
            temporary.replace(destination)
            # Keep separate difficulty profiles; crossing a ladder band does
            # not replace the only cached snapshot of every previous band.
            for cutoff in {snapshot.rating, requested_rating} - {None}:
                profile = self.cache_dir / f"{snapshot.format_id}-{cutoff}.json"
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.cache_dir, prefix="meta-", suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(record, handle, ensure_ascii=False, separators=(",", ":"))
                temporary.replace(profile)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
