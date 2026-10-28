"""Local, bounded experience memory and deterministic Portuguese postmortems.

This is contextual calibration from observed transitions, not reinforcement
learning over simulated counterfactuals. It cannot prove which alternative
would have won. Every completed match is recorded; disabled learning never
adds evidence. No credentials, team text or full configuration are stored.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

MIN_BATTLES = 3
MAX_ADJUSTMENT = 0.75
MAX_CONTEXT_SAMPLES = 200
MAX_TOTAL_SAMPLES = 100_000
HEAL_MOVES = {"recover", "roost", "slackoff", "softboiled", "milkdrink", "shoreup", "synthesis", "moonlight", "morningsun", "rest", "healorder", "strengthsap"}
PRIVATE_KEYS = {"password", "token", "secret", "authorization", "cookie", "config", "configuration", "team"}
STATE_KEYS = {"schema_version", "turn", "active", "active_id", "active_hp", "own_hp_total", "own_alive",
              "foe_active", "foe_id", "foe_hp", "foe_hp_total", "foe_alive", "own_hp", "foe_hp_by_id",
              "own_status", "foe_status", "own_boosts", "foe_boosts", "known_foe_moves", "own_hazards",
              "foe_hazards", "weather", "fields", "force_switch", "finished", "can_tera",
              "own_roster", "foe_roster", "preview_foes"}


def team_fingerprint(team_text: str, format_id: str = "") -> str:
    """Stable identity; different EV/IV/move exports get separate memories."""
    if format_id == "gen9randombattle":
        return "random"
    normalized = team_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _unit(value: Any) -> float | None:
    number = _number(value)
    return min(1.0, max(0.0, number)) if number is not None else None


def _text(value: Any, limit: int = 200) -> str:
    return str(value or "")[:limit].replace("\x00", "")


def _safe(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {_text(key, 80): _safe(item, depth + 1) for key, item in list(value.items())[:80]
                if str(key).lower() not in PRIVATE_KEYS}
    if isinstance(value, (tuple, list)):
        return [_safe(item, depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return _text(value, 1200)
    if value is None or isinstance(value, bool):
        return value
    return _number(value)


def _namespace(value: Any) -> str:
    raw = _text(value or "showdown", 250)
    if "://" in raw:
        parsed = urlsplit(raw)
        return (parsed.hostname or "custom") + (f":{parsed.port}" if parsed.port else "")
    # Namespaces are identifiers, never login-bearing URLs.
    return raw.split("@")[-1].replace("\n", "").replace("\r", "")


def _decision(raw: dict) -> dict:
    kept = {key: _safe(raw[key]) for key in ("turn", "context_key", "action_key", "chosen", "score", "features", "diagnostics", "observations", "local_outcome", "candidates") if key in raw}
    for state in ("before", "after"):
        supplied = raw.get(state)
        kept[state] = {key: _safe(value) for key, value in supplied.items() if key in STATE_KEYS} if isinstance(supplied, dict) else {}
    return kept


def _outcome(decision: dict) -> dict:
    """Only compare identity-matched HP; a switch is never counted as damage."""
    observed = decision.get("local_outcome") or {}
    if observed:
        received, dealt = _unit(observed.get("received")), _unit(observed.get("dealt"))
        return {"received": received, "dealt": dealt, "own_ko": bool(observed.get("own_ko")),
                "foe_ko": bool(observed.get("foe_ko")),
                "reliable": observed.get("reliable") is True and received is not None and dealt is not None}
    before, after = decision.get("before", {}), decision.get("after", {})
    own_before = _unit(before.get("active_hp"))
    own_after = _unit(after.get("active_hp"))
    foe_before, foe_after = _unit(before.get("foe_hp")), _unit(after.get("foe_hp"))
    own_same = bool(before.get("active")) and before.get("active") == after.get("active")
    foe_same = bool(before.get("foe_active")) and before.get("foe_active") == after.get("foe_active")
    # Identity maps are stronger evidence when a pivot changes the active slot.
    for mapping, identity, own in (("own_hp", before.get("active"), True), ("foe_hp_by_id", before.get("foe_active"), False)):
        first, second = before.get(mapping), after.get(mapping)
        if identity and isinstance(first, dict) and isinstance(second, dict) and identity in first and identity in second:
            left, right = _unit(first[identity]), _unit(second[identity])
            if own:
                own_before, own_after, own_same = left, right, True
            else:
                foe_before, foe_after, foe_same = left, right, True
    reliable = own_same and foe_same and all(value is not None for value in (own_before, own_after, foe_before, foe_after))
    flags = decision.get("observations") or {}
    return {"received": max(0, own_before - own_after) if own_before is not None and own_after is not None and own_same else None,
            "dealt": max(0, foe_before - foe_after) if foe_before is not None and foe_after is not None and foe_same else None,
            "own_ko": bool(flags.get("own_fainted")), "foe_ko": bool(flags.get("foe_fainted")), "reliable": bool(reliable)}


def _observed_moves(decisions: list[dict]) -> set[tuple[str, str]]:
    """Learn revealed move presence even when a move misses or scores no damage."""
    result = set()
    for decision in decisions:
        before, after = decision.get("before") or {}, decision.get("after") or {}
        roster = {**before.get("foe_roster", {}), **after.get("foe_roster", {})}
        for entry in (decision.get("observations") or {}).get("opponent_moves", []):
            actor, move = entry.get("actor"), entry.get("move")
            species = roster.get(actor, {}).get("species")
            if species and move and len(move) <= 40 and move.isalnum():
                result.add((str(species), str(move)))
    return result


def _calibration_sample(decision: dict) -> dict | None:
    """Direct attack damage only, with the same identified target and no RNG event.

    Samples diagnose uncertain forecasts. They are not proof that the action
    was wrong, and never replace the battle simulator's damage mechanics.
    """
    flags, outcome = decision.get("observations") or {}, decision.get("local_outcome") or {}
    diagnostic, before = decision.get("diagnostics") or {}, decision.get("before") or {}
    if (not outcome.get("reliable") or any(flags.get(key) for key in
            ("miss", "critical", "invalid_choice", "own_failed", "own_immune", "foe_switched"))):
        return None
    action = str(decision.get("action_key", ""))
    if not action.startswith("move:") or flags.get("last_move_own") != action.split(":")[1]:
        return None
    direct = _unit(outcome.get("direct_dealt"))
    expected = _unit(diagnostic.get("expected_dealt"))
    low, high = _unit(diagnostic.get("damage_low")), _unit(diagnostic.get("damage_high"))
    if direct is None or expected is None or low is None or high is None or high <= 0:
        return None
    hp = _unit(before.get("foe_hp")) or 1.0
    expected, low, high = min(hp, expected), min(hp, low), min(hp, high)
    return {"observed": direct, "expected": expected, "low": low, "high": high,
            "error": round(direct - expected, 6), "outside_range": direct < low - 0.02 or direct > high + 0.02}


def _policy_samples(decision: dict) -> list[dict]:
    """Credit measured feature errors, never blame all actions for a lost game.

    Only the new policy's forecast can calibrate its weights. Random outcomes,
    newly revealed coverage, pivots, residual damage and censored KO damage
    are excluded from damage calibration. Utility features require an observed
    corresponding state transition. This is bounded correlation, not causal
    attribution or a claim that an unplayed alternative would have won.
    """
    diagnostic, outcome = decision.get("diagnostics") or {}, decision.get("local_outcome") or {}
    flags, features = decision.get("observations") or {}, decision.get("features") or {}
    before, after = decision.get("before") or {}, decision.get("after") or {}
    context = diagnostic.get("policy_context")
    if diagnostic.get("policy_version") != 4 or not isinstance(context, str) or not context.startswith("p4:"):
        return []
    if not outcome.get("reliable") or any(flags.get(key) for key in
            ("miss", "critical", "invalid_choice", "information_revealed", "own_switched", "foe_switched")):
        return []
    if before.get("force_switch") or not str(decision.get("action_key", "")).startswith("move:"):
        return []
    result = []
    def sample(feature, observed, predicted, direction=1.):
        active = _number(features.get("policy_" + feature))
        observed, predicted = _number(observed), _number(predicted)
        if active is None or abs(active) < .03 or observed is None or predicted is None:
            return
        residual = max(-1., min(1., (observed - predicted) * direction))
        if abs(residual) < .03:
            return
        result.append({"context_key": context[:80], "feature": feature,
                       "residual": round(residual, 6), "exposure": round(abs(active), 6)})
    if not flags.get("residual_or_item") and not flags.get("own_failed") and not flags.get("own_immune"):
        if not outcome.get("foe_ko"):
            sample("damage", outcome.get("direct_dealt"), diagnostic.get("expected_dealt"))
        if not outcome.get("own_ko"):
            sample("incoming", outcome.get("direct_received"), diagnostic.get("expected_received"), -1.)
        sample("ko", float(bool(outcome.get("foe_ko"))), diagnostic.get("ko_chance"))
        sample("faint_risk", float(bool(outcome.get("own_ko"))), diagnostic.get("faint_risk"), -1.)
        if before.get("active_id") == after.get("active_id") and _unit(outcome.get("direct_received")) is not None:
            healed = max(0., (_unit(after.get("active_hp")) or 0.) - (_unit(before.get("active_hp")) or 0.) + outcome["direct_received"])
            sample("heal", healed, features.get("policy_heal"))
    # An executed setup that ends in a direct KO cannot realize its forecast.
    move = str(decision.get("action_key", "")).split(":")[1]
    if outcome.get("own_ko") and flags.get("last_move_own") == move and not flags.get("residual_or_item"):
        sample("setup", 0., features.get("policy_setup"))
    return result


def _base_species(species: str) -> str:
    from .strategy import base_species_id
    try:
        return base_species_id(species or "")
    except Exception:
        return str(species or "")


def _learnable_action(action: str) -> bool:
    """HP swing only measures attacks and switches.

    Status, hazard and setup moves never deal immediate damage, and pivots
    (U-turn, Volt Switch) hand the next hit to a different Pokémon, so their
    swing would teach the bot to stop setting hazards or pivoting.
    """
    if action.startswith("switch:"):
        return True
    if not action.startswith("move:"):
        return False
    from poke_env.data import GenData
    move = GenData.from_gen(9).moves.get(action.split(":")[1], {})
    return move.get("category") in {"Physical", "Special"} and not move.get("selfSwitch")


def _matchup_samples(decisions: list[dict]) -> list[dict]:
    """Observed swing of one action in one matchup, excluding pure luck.

    swing = direct damage dealt - direct damage received (+/- KOs), measured
    on the protocol. Misses, crits and forced replacements are excluded so a
    good play is not punished for a bad roll.
    """
    samples = []
    for index, decision in enumerate(decisions):
        before, flags = decision.get("before") or {}, decision.get("observations") or {}
        outcome = decision.get("local_outcome") or {}
        action = str(decision.get("action_key", ""))
        if (not action or not _learnable_action(action) or before.get("force_switch") or not outcome.get("reliable")
                or any(flags.get(key) for key in ("miss", "critical", "invalid_choice"))):
            continue
        own, foe = _base_species(before.get("active", "")), _base_species(before.get("foe_active", ""))
        if not own or not foe:
            continue
        dealt = _unit(outcome.get("direct_dealt", outcome.get("dealt"))) or 0.
        received = _unit(outcome.get("direct_received", outcome.get("received"))) or 0.
        swing = dealt - received + .6 * (bool(outcome.get("foe_ko")) - bool(outcome.get("own_ko")))
        samples.append({"decision_index": index, "own_species": own, "foe_species": foe,
                        "action_key": action[:200], "swing": round(max(-1.6, min(1.6, swing)), 5)})
    return samples


def _progress_analysis(decisions: list[dict], learning_enabled: bool) -> tuple[list[dict], list[dict]]:
    """Share verified transition facts with the policy used during battle."""
    from .progress import transition_evidence
    findings, updates = [], []
    titles = {"setup_phazed": "Setup undone by phazing",
              "setup_ko": "Setup lost to a knockout before it paid off",
              "damaging_cycle": "Cycle without progress, losing HP",
              "failed_action": "Repeated failure condition"}
    for index, decision in enumerate(decisions):
        for evidence in transition_evidence(decision, decisions[:index]):
            kind = _text(evidence.get("kind"), 80)
            pattern, action = _text(evidence.get("pattern_key"), 1200), _text(evidence.get("action_key"), 200)
            if not kind or not pattern or not action:
                continue
            reason = _text(evidence.get("reason"), 1200)
            updates.append({"decision_index": index, "turn": decision.get("turn", 0),
                            "kind": kind, "pattern_key": pattern, "action_key": action,
                            "counter_move": _text(evidence.get("counter_move"), 80),
                            "reason": reason, "proof": _safe(evidence.get("proof") or {}),
                            "learned": bool(learning_enabled)})
            findings.append({"turn": int(_number(decision.get("turn")) or 0),
                             "action": _text(decision.get("chosen") or action), "kind": kind,
                             "title": titles.get(kind, "Progress depends on the observed state"),
                             "detail": reason,
                             "hypothesis": "Check the revealed counter and look for lasting gain in this context; the same action is still valid once conditions change."})
    return findings, updates


def _inspect(decision: dict, previous: list[dict]) -> tuple[list[dict], float | None, str]:
    features, diagnostic = decision.get("features") or {}, decision.get("diagnostics") or {}
    flags = decision.get("observations") or {}
    before = decision.get("before") or {}
    outcome = _outcome(decision)
    turn = int(_number(decision.get("turn")) or 0)
    action = _text(decision.get("chosen") or decision.get("action_key"), 200)
    findings = []

    def add(kind: str, title: str, detail: str, hypothesis: str = ""):
        findings.append({"turn": turn, "action": action, "kind": kind, "title": title,
                         "detail": detail, "hypothesis": hypothesis})

    random_event = bool(flags.get("miss") or flags.get("critical"))
    if random_event:
        what = "a miss" if flags.get("miss") else "a critical hit"
        if flags.get("miss") and flags.get("critical"):
            what = "a miss and a critical hit"
        add("randomness", "Random event", f"The protocol recorded {what}; this transition does not change the action memory.", "Tell known risk apart from a single lucky or unlucky result.")
    if flags.get("information_revealed"):
        add("new_information", "New information during the turn", "Information was revealed during the transition; earlier predictions may have been incomplete.")
    if flags.get("residual_or_item"):
        add("residual", "Indirect damage or healing", "The protocol recorded a residual or item effect; the net HP change is not just the move's damage.")
    if not outcome["reliable"]:
        add("incomplete", "Transition not reliably comparable", "HP or identity could not be compared safely, or the capture is intermediate; no learning signal is created.")
        return findings, None, "incomplete data or non-comparable transition"

    received, dealt = outcome["received"], outcome["dealt"]
    expected = _unit(diagnostic.get("expected_received"))
    predicted_dealt = _unit(diagnostic.get("expected_dealt"))
    faint_risk = _unit(diagnostic.get("faint_risk"))
    own_ko, foe_ko = outcome["own_ko"], outcome["foe_ko"]
    if own_ko and faint_risk is not None and faint_risk >= 0.55:
        add("known_risk", "Knockout with a foreseen risk", f"The Pokémon fainted; the estimated risk before the decision was {faint_risk:.0%}. The exchange dealt {dealt:.0%} and took {received:.0%}.", "Re-examine options that keep this Pokémon alive in similar spots; a sack may have been intentional.")
    if expected is not None and received > expected + 0.25:
        add("damage_surprise", "Took more damage than expected", f"Took {received:.0%}; predicted {expected:.0%}. A hidden set, status, residual effects or calculation error may explain it.", "Review the damage estimate for this matchup without blaming the whole gap on the move choice.")
    if predicted_dealt is not None and predicted_dealt > dealt + 0.25:
        add("damage_shortfall", "Dealt less damage than expected", f"Dealt {dealt:.0%}; predicted {predicted_dealt:.0%}. Healing, a switch, hidden information or a failure may explain part of it.", "Check the coverage and the assumptions about the opposing set.")
    move_id = action.lower().split(" + ")[0].replace(" ", "")
    is_switch = bool(features.get("switch")) or _text(decision.get("action_key")).startswith("switch:")
    is_utility = any((_number(features.get(key)) or 0) > 0 for key in ("heal", "setup", "hazards", "removal", "status", "pivot")) or move_id in HEAL_MOVES
    wasted_heal = move_id in HEAL_MOVES and (_unit(before.get("active_hp")) or 0) >= 0.98 and received <= 0.02
    if wasted_heal:
        add("low_value_heal", "Healing with nothing to heal", "The healing move started at almost full HP and no meaningful damage was taken.", "Prefer this heal less at high HP.")
    if own_ko and (_number(features.get("setup")) or 0) > 0:
        add("setup_fainted", "Setup followed by a knockout", "A stat boost was chosen and the Pokémon fainted in the same transition; the boost never paid off.", "Require more survival margin before setting up in this matchup again.")
    hazards = _unit(diagnostic.get("entry_hazards")) or 0
    recent_switches = sum(bool((item.get("features") or {}).get("switch")) for item in previous[-2:])
    if is_switch and (hazards >= 0.125 or recent_switches >= 2):
        add("switch_cost", "Cost of switching", f"Switched in with an estimated {hazards:.0%} hazard damage; {recent_switches} switches in the two previous decisions.", "Re-evaluate switch cycles and chances to remove hazards; the switch may still be needed.")
    if flags.get("immune"):
        add("immunity", "Immunity", "The protocol recorded an immunity in this transition. It does not prove the immunity was known before the choice.", "Use the revealed information when weighing coverage in similar matchups.")
    elif flags.get("failed"):
        add("failed", "Action had no effect", "The protocol recorded a failure; check the move's conditions and whether the target changed.", "Avoid repeating the same failure once there is enough evidence.")

    if random_event:
        return findings, None, "miss/critical: result not attributed to the choice"
    if diagnostic.get("forced_switch"):
        return findings, None, "forced switch: a replacement is not an attacking turn"
    # Utility choices may pay off several turns later: do not punish every
    # setup, hazard, status or pivot just for causing no immediate damage.
    reward = 2.0 * (int(foe_ko) - int(own_ko))
    if not is_utility and not is_switch:
        reward += 4.0 * (dealt - received)
    if is_switch:
        reward -= 2.0 * hazards
    if wasted_heal:
        reward -= 1.5
    if flags.get("own_immune") and dealt <= 0.01:
        reward -= 1.0
    elif flags.get("own_failed") and dealt <= 0.01:
        reward -= 0.75
    if flags.get("information_revealed") or flags.get("residual_or_item"):
        reward *= 0.5
    reward = round(max(-6.0, min(6.0, reward)), 4)
    if abs(reward) < 0.15:
        return findings, None, "neutral immediate effect or unmeasured future benefit"
    reason = f"Observed exchange: dealt {dealt:.0%}, took {received:.0%}, own knockout={own_ko}, opposing knockout={foe_ko}; signal {reward:+.2f}."
    return findings, reward, reason



HISTORY_KEYS = ("battle_id", "battle_tag", "storage_id", "server_namespace", "username", "ended_at", "format_id", "team_id",
                "opponent", "result", "turns", "rating_before", "rating_after", "opponent_rating", "opponent_rank", "own_rank",
                "opponent_pokemon", "analysis_path", "json_path", "replay_path", "summary", "learning_enabled")

class ExperienceStore:
    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "data" / "experience.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS battles (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL,
                    ended_at TEXT NOT NULL, format_id TEXT NOT NULL, team_id TEXT NOT NULL,
                    username TEXT NOT NULL, opponent TEXT NOT NULL, result TEXT NOT NULL,
                    turns INTEGER NOT NULL, report_json TEXT NOT NULL,
                    PRIMARY KEY(server_namespace, battle_id)
                );
                CREATE TABLE IF NOT EXISTS action_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL,
                    decision_index INTEGER NOT NULL, format_id TEXT NOT NULL,
                    team_id TEXT NOT NULL, context_key TEXT NOT NULL, action_key TEXT NOT NULL,
                    reward REAL NOT NULL, reason TEXT NOT NULL,
                    UNIQUE(server_namespace, battle_id, decision_index),
                    FOREIGN KEY(server_namespace, battle_id) REFERENCES battles(server_namespace, battle_id)
                );
                CREATE INDEX IF NOT EXISTS evidence_context ON action_evidence(format_id,team_id,context_key,action_key);
                CREATE INDEX IF NOT EXISTS battles_date ON battles(ended_at DESC);
                CREATE TABLE IF NOT EXISTS observed_moves (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL,
                    format_id TEXT NOT NULL, species TEXT NOT NULL, move_id TEXT NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id,species,move_id)
                );
                CREATE INDEX IF NOT EXISTS observed_moves_species ON observed_moves(format_id,species);
                CREATE TABLE IF NOT EXISTS forecast_evidence (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL, decision_index INTEGER NOT NULL,
                    format_id TEXT NOT NULL, team_id TEXT NOT NULL, context_key TEXT NOT NULL, action_key TEXT NOT NULL,
                    observed REAL NOT NULL, expected REAL NOT NULL, error REAL NOT NULL,
                    outside_range INTEGER NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id,decision_index)
                );
                CREATE INDEX IF NOT EXISTS forecast_context ON forecast_evidence(format_id,team_id,context_key,action_key);
                CREATE TABLE IF NOT EXISTS progress_evidence (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL,
                    decision_index INTEGER NOT NULL, format_id TEXT NOT NULL, team_id TEXT NOT NULL,
                    pattern_key TEXT NOT NULL, action_key TEXT NOT NULL, kind TEXT NOT NULL,
                    counter_move TEXT NOT NULL, proof_json TEXT NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id,decision_index,kind),
                    FOREIGN KEY(server_namespace,battle_id) REFERENCES battles(server_namespace,battle_id)
                );
                CREATE INDEX IF NOT EXISTS progress_context ON progress_evidence(format_id,team_id,pattern_key,action_key);
                CREATE TABLE IF NOT EXISTS policy_evidence (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL, decision_index INTEGER NOT NULL,
                    format_id TEXT NOT NULL, team_id TEXT NOT NULL, context_key TEXT NOT NULL,
                    feature TEXT NOT NULL, residual REAL NOT NULL, exposure REAL NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id,decision_index,feature),
                    FOREIGN KEY(server_namespace,battle_id) REFERENCES battles(server_namespace,battle_id)
                );
                CREATE INDEX IF NOT EXISTS policy_context ON policy_evidence(format_id,team_id,context_key,feature);
                CREATE TABLE IF NOT EXISTS battle_summary (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL, summary_json TEXT NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id)
                );
                CREATE TABLE IF NOT EXISTS history_archive (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id),
                    FOREIGN KEY(server_namespace,battle_id) REFERENCES battles(server_namespace,battle_id)
                );
                CREATE TABLE IF NOT EXISTS matchup_evidence (
                    server_namespace TEXT NOT NULL, battle_id TEXT NOT NULL, decision_index INTEGER NOT NULL,
                    format_id TEXT NOT NULL, team_id TEXT NOT NULL, own_species TEXT NOT NULL,
                    foe_species TEXT NOT NULL, action_key TEXT NOT NULL, swing REAL NOT NULL, won INTEGER NOT NULL,
                    PRIMARY KEY(server_namespace,battle_id,decision_index)
                );
                CREATE INDEX IF NOT EXISTS matchup_lookup ON matchup_evidence(format_id,team_id);
                CREATE TABLE IF NOT EXISTS store_flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            # Reports hold the whole battle log (often MBs). The history list only
            # needs a few fields, kept in battle_summary by triggers on every write.
            summary = "json_object(" + ",".join(f"'{key}', json(NEW.report_json -> '$.{key}')" for key in HISTORY_KEYS) + ")"
            db.executescript(f"""
                CREATE TRIGGER IF NOT EXISTS battle_summary_insert AFTER INSERT ON battles BEGIN
                    INSERT OR REPLACE INTO battle_summary VALUES (NEW.server_namespace, NEW.battle_id, {summary}); END;
                CREATE TRIGGER IF NOT EXISTS battle_summary_update AFTER UPDATE OF report_json ON battles BEGIN
                    INSERT OR REPLACE INTO battle_summary VALUES (NEW.server_namespace, NEW.battle_id, {summary}); END;
                CREATE TRIGGER IF NOT EXISTS battle_summary_delete AFTER DELETE ON battles BEGIN
                    DELETE FROM battle_summary WHERE server_namespace=OLD.server_namespace AND battle_id=OLD.battle_id; END;
            """)
            db.execute("INSERT OR IGNORE INTO battle_summary SELECT server_namespace, battle_id, "
                       + summary.replace("NEW.", "") + " FROM battles WHERE NOT EXISTS (SELECT 1 FROM battle_summary s "
                       "WHERE s.server_namespace=battles.server_namespace AND s.battle_id=battles.battle_id)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=15000")
            with connection:
                yield connection
        finally:
            connection.close()

    def adjustment(self, format_id: str, team_id: str, context_key: str, action_key: str) -> tuple[float, str]:
        if not all((format_id, team_id, context_key, action_key)):
            return 0.0, "Memory without full context."
        with self._connect() as db:
            rows = db.execute("SELECT reward,server_namespace,battle_id FROM action_evidence WHERE format_id=? AND team_id=? AND context_key=? AND action_key=? ORDER BY id DESC LIMIT ?",
                              (format_id, team_id, context_key, action_key, MAX_CONTEXT_SAMPLES)).fetchall()
        distinct = len({(row["server_namespace"], row["battle_id"]) for row in rows})
        if distinct < MIN_BATTLES:
            return 0.0, f"Memory: {distinct}/{MIN_BATTLES} comparable battles; adjustment still off."
        # Each battle has equal weight, so a single long match cannot dominate.
        grouped: dict[tuple[str, str], list[float]] = {}
        for row in rows:
            grouped.setdefault((row["server_namespace"], row["battle_id"]), []).append(row["reward"])
        mean = sum(sum(values) / len(values) for values in grouped.values()) / distinct
        adjustment = max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, 2 * mean * distinct / (distinct + 8)))
        return round(adjustment, 3), f"Local memory: {distinct} battles, {len(rows)} transitions; adjustment {adjustment:+.2f} points (limit ±{MAX_ADJUSTMENT:g}). An observed correlation, not proof of a better alternative."

    def policy_weights(self, format_id: str, team_id: str) -> dict:
        """Load a compact read-only snapshot outside the per-candidate loop.

        Each independent battle has one vote per context/feature. Repeated
        turns from a single stalled match cannot satisfy the three-game gate.
        Only new policy evidence participates; legacy rewards remain intact.
        """
        from .policy import LEARNABLE, MAX_WEIGHT_DELTA
        with self._connect() as db:
            rows = db.execute("SELECT server_namespace,battle_id,context_key,feature,residual,exposure "
                              "FROM policy_evidence WHERE format_id=? AND team_id=? ORDER BY rowid DESC LIMIT ?",
                              (format_id, team_id, MAX_TOTAL_SAMPLES)).fetchall()
        groups = {}
        for row in rows:
            if row["feature"] not in LEARNABLE:
                continue
            group = groups.setdefault((row["context_key"], row["feature"]), {})
            group.setdefault((row["server_namespace"], row["battle_id"]), []).append(
                (float(row["residual"]), float(row["exposure"])))
        contexts = {}
        for (context, feature), battles in groups.items():
            if len(battles) < MIN_BATTLES:
                continue
            means = [sum(residual * weight for residual, weight in values) /
                     max(.01, sum(weight for _, weight in values)) for values in battles.values()]
            confidence = len(means) / (len(means) + 12)
            delta = max(-MAX_WEIGHT_DELTA, min(MAX_WEIGHT_DELTA, MAX_WEIGHT_DELTA * confidence * sum(means) / len(means)))
            contexts.setdefault(context, {})[feature] = round(delta, 6)
        return {"version": 4, "format_id": format_id, "team_id": team_id, "contexts": contexts}

    @staticmethod
    def _store_matchups(db, namespace: str, battle_id: str, format_id: str, team_id: str,
                        samples: list[dict], won: bool) -> int:
        inserted = 0
        for sample in samples:
            inserted += db.execute("INSERT OR IGNORE INTO matchup_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                (namespace, battle_id, sample["decision_index"], format_id, team_id, sample["own_species"],
                 sample["foe_species"], sample["action_key"], sample["swing"], int(won))).rowcount
        if inserted:
            db.execute("DELETE FROM matchup_evidence WHERE rowid NOT IN (SELECT rowid FROM matchup_evidence ORDER BY rowid DESC LIMIT ?)", (MAX_TOTAL_SAMPLES,))
        return inserted

    def _backfill_matchups(self) -> None:
        """One-time import of matchup outcomes from battles recorded earlier."""
        with self._connect() as db:
            if db.execute("SELECT 1 FROM store_flags WHERE name='matchup_backfill_v1'").fetchone():
                return
            rows = db.execute("SELECT server_namespace,battle_id,format_id,team_id,result,report_json FROM battles").fetchall()
            for row in rows:
                try:
                    report = json.loads(row["report_json"])
                except ValueError:
                    continue
                if not report.get("learning_enabled", True):
                    continue
                decisions = [item for item in report.get("decisions", []) if isinstance(item, dict)]
                self._store_matchups(db, row["server_namespace"], row["battle_id"], row["format_id"], row["team_id"],
                                     _matchup_samples(decisions), row["result"] == "win")
            db.execute("INSERT OR REPLACE INTO store_flags VALUES ('matchup_backfill_v1', ?)",
                       (datetime.now(timezone.utc).isoformat(),))

    def matchup_memory(self, format_id: str, team_id: str) -> dict:
        """Bounded biases for (own species, foe species, action) from real games.

        Each battle is one vote; at least three independent battles with a
        consistent result are required. Losing patterns weigh more than
        winning ones: the goal is to stop repeating plays that cost HP.
        """
        self._backfill_matchups()
        with self._connect() as db:
            rows = db.execute("SELECT server_namespace,battle_id,own_species,foe_species,action_key,swing "
                              "FROM matchup_evidence WHERE format_id=? AND team_id=? ORDER BY rowid DESC LIMIT ?",
                              (format_id, team_id, MAX_TOTAL_SAMPLES)).fetchall()
        groups: dict[tuple, dict] = {}
        for row in rows:
            key = (row["own_species"], row["foe_species"], row["action_key"])
            groups.setdefault(key, {}).setdefault((row["server_namespace"], row["battle_id"]), []).append(row["swing"])
        memory = {}
        for key, battles in groups.items():
            if len(battles) < MIN_BATTLES or not _learnable_action(key[2]):
                continue
            means = [sum(values) / len(values) for values in battles.values()]
            mean = sum(means) / len(means)
            agreement = sum((value < 0) == (mean < 0) for value in means) / len(means)
            if abs(mean) < .08 or agreement < .66:
                continue
            confidence = len(means) / (len(means) + 4)
            bias = max(-16., min(8., (70. if mean < 0 else 35.) * mean * confidence))
            memory[key] = {"bias": round(bias, 3), "battles": len(means), "mean_swing": round(mean, 4),
                           "reason": (f"Memory: this move in this matchup averaged {mean:+.0%} HP "
                                      f"over {len(means)} battles.")}
        return memory

    def statistics(self, format_id: str | None = None, username: str | None = None,
                   server_namespace: str | None = None) -> dict:
        """Aggregate the complete history, independently of the scroll-list limit."""
        clauses = ["NOT EXISTS (SELECT 1 FROM history_archive h WHERE h.server_namespace=battles.server_namespace AND h.battle_id=battles.battle_id)"]
        parameters = []
        if format_id:
            clauses.append("format_id=?")
            parameters.append(format_id)
        if username:
            clauses.append("username=? COLLATE NOCASE")
            parameters.append(username)
        if server_namespace:
            namespace = _namespace(server_namespace)
            official = namespace == "showdown" or (urlsplit("//" + namespace).hostname or "").endswith(".psim.us")
            if official:
                clauses.append("(server_namespace='showdown' OR server_namespace LIKE '%.psim.us' OR server_namespace LIKE '%.psim.us:%')")
            elif (urlsplit("//" + namespace).hostname or "") in {"localhost", "127.0.0.1"}:
                port = urlsplit("//" + namespace).port
                suffix = f":{port}" if port is not None else ""
                clauses.append("server_namespace IN (?,?)")
                parameters.extend(["localhost" + suffix, "127.0.0.1" + suffix])
            else:
                clauses.append("server_namespace=?")
                parameters.append(namespace)
        with self._connect() as db:
            rows = db.execute("SELECT result,COUNT(*) AS count FROM battles" +
                              (" WHERE " + " AND ".join(clauses) if clauses else "") + " GROUP BY result", parameters).fetchall()
        counts = {row["result"]: row["count"] for row in rows}
        wins, losses, ties = counts.get("win", 0), counts.get("loss", 0), counts.get("tie", 0)
        total = wins + losses + ties
        return {"wins": wins, "losses": losses, "ties": ties, "total": total,
                "winrate": round(100 * wins / (wins + losses), 2) if wins + losses else 0.}

    def preferences(self, format_id: str, species: str | None = None, *, rating: float | None = None,
                    username: str | None = None, server_namespace: str | None = None) -> dict:
        """Return observed opponent move priors for search's uncertainty model.

        Frequencies describe move presence among observed battles. They are
        not a reconstructed joint set and must be blended with usage data,
        never substituted for the current opponent's revealed information.
        """
        with self._connect() as db:
            # Ratings are read once per battle (reports hold the whole log), not per move row.
            rows = db.execute("WITH b AS MATERIALIZED (SELECT server_namespace,battle_id,username,"
                              "json_extract(report_json,'$.opponent_rating') AS opponent_rating,"
                              "json_extract(report_json,'$.rating_before') AS rating_before,"
                              "json_extract(report_json,'$.rating_after') AS rating_after FROM battles "
                              "WHERE format_id=?) "
                              "SELECT o.species,o.move_id,o.server_namespace,o.battle_id,b.username,"
                              "b.opponent_rating,b.rating_before,b.rating_after "
                              "FROM observed_moves o JOIN b ON o.server_namespace=b.server_namespace AND o.battle_id=b.battle_id "
                              "WHERE o.format_id=?" + (" AND o.species=?" if species else ""),
                              (format_id, format_id, species) if species else (format_id, format_id)).fetchall()
        grouped = {}
        for row in rows:
            if username is not None and row["username"].casefold() != username.casefold():
                continue
            if server_namespace is not None and row["server_namespace"] != server_namespace:
                official = lambda value: value == "showdown" or (urlsplit("//" + value).hostname or "").endswith(".psim.us")
                if not (official(server_namespace) and official(row["server_namespace"])):
                    continue
            if rating is not None:
                from .meta import rating_bucket
                seen_rating = next((_number(row[key]) for key in ("opponent_rating", "rating_before", "rating_after")
                                    if _number(row[key]) is not None), None)
                if seen_rating is None or rating_bucket(seen_rating) != rating_bucket(rating):
                    continue
            info = grouped.setdefault(row["species"], {"battles": set(), "moves": {}})
            ident = (row["server_namespace"], row["battle_id"])
            info["battles"].add(ident)
            info["moves"].setdefault(row["move_id"], set()).add(ident)
        output = {}
        for name, info in grouped.items():
            count = len(info["battles"])
            output[name] = {"battles": count, "confidence": round(count / (count + 8), 4),
                            "moves": {move: round(len(seen) / count, 4) for move, seen in info["moves"].items()}}
        return {"version": 2, "format_id": format_id, "species": output}

    def calibration(self, format_id: str, team_id: str, context_key: str, action_key: str) -> dict:
        """Comparable repeated forecast errors; zero confidence before three battles."""
        with self._connect() as db:
            rows = db.execute("SELECT server_namespace,battle_id,error,outside_range FROM forecast_evidence WHERE format_id=? AND team_id=? AND context_key=? AND action_key=? ORDER BY rowid DESC LIMIT ?",
                              (format_id, team_id, context_key, action_key, MAX_CONTEXT_SAMPLES)).fetchall()
        grouped = {}
        for row in rows:
            grouped.setdefault((row["server_namespace"], row["battle_id"]), []).append(row)
        count = len(grouped)
        average_errors = [sum(row["error"] for row in group) / len(group) for group in grouped.values()]
        outside = sum(bool(row["outside_range"]) for row in rows) / len(rows) if rows else 0.0
        return {"version": 2, "battles": count, "samples": len(rows),
                "confidence": round(count / (count + 8), 4) if count >= MIN_BATTLES else 0.0,
                "bias": round(sum(average_errors) / count, 4) if count else 0.0,
                "absolute_error": round(sum(abs(error) for error in average_errors) / count, 4) if count else 0.0,
                "outside_range_fraction": round(outside, 4)}

    def progress_memory(self, format_id: str, team_id: str, pattern_key: str | None = None,
                        action_key: str | None = None) -> list[dict]:
        """Conditional observed counters, never a blanket dislike of a move.

        Several cycles in one battle count as one independent battle. The
        proof/confidence confirm what happened; independent_battle_confidence
        describes support across encounters, not the chance of the next move.
        Tactical safety and the current opponent's revealed state must still
        be checked by the online policy before using an entry.
        """
        if not format_id or not team_id:
            return []
        clauses, values = ["format_id=?", "team_id=?"], [format_id, team_id]
        for field, value in (("pattern_key", pattern_key), ("action_key", action_key)):
            if value is not None:
                clauses.append(field + "=?")
                values.append(value)
        with self._connect() as db:
            rows = db.execute("SELECT * FROM progress_evidence WHERE " + " AND ".join(clauses)
                              + " ORDER BY rowid DESC LIMIT ?", (*values, MAX_TOTAL_SAMPLES)).fetchall()
        groups = {}
        for row in rows:
            identity = (row["server_namespace"], row["battle_id"])
            key = (row["pattern_key"], row["action_key"], row["kind"], row["counter_move"])
            group = groups.setdefault(key, {"battles": set(), "observations": 0,
                                             "proof": json.loads(row["proof_json"])})
            group["battles"].add(identity)
            group["observations"] += 1
        patterns = []
        for (key, action, kind, counter), group in groups.items():
            distinct = len(group["battles"])
            patterns.append({"pattern_key": key, "action_key": action, "kind": kind,
                             "counter_move": counter, "observations": group["observations"],
                             "battles": distinct, "confidence": 1.0,
                             "independent_battle_confidence": round(distinct / (distinct + 4), 4),
                             "proof": group["proof"],
                             "reason": "Counter or cycle observed in this context; the current decision still has to check safety and progress."})
        return patterns

    @staticmethod
    def _store_progress(db, record: dict, updates: list[dict]) -> int:
        inserted = 0
        for item in updates:
            if not item.get("learned"):
                continue
            inserted += db.execute("INSERT OR IGNORE INTO progress_evidence VALUES (?,?,?,?,?,?,?,?,?,?)",
                (record["server_namespace"], record["battle_id"], item["decision_index"],
                 record["format_id"], record["team_id"], item["pattern_key"], item["action_key"],
                 item["kind"], item["counter_move"], json.dumps(item["proof"], ensure_ascii=False))).rowcount
        if inserted:
            db.execute("DELETE FROM progress_evidence WHERE rowid NOT IN (SELECT rowid FROM progress_evidence ORDER BY rowid DESC LIMIT ?)", (MAX_TOTAL_SAMPLES,))
        return inserted

    def reanalyze_progress(self, battle_id: str, server_namespace: str | None = None) -> dict | None:
        """Backfill one recorded encounter without replaying older learning.

        The stored decisions are authoritative. Original report files receive
        a single backup before updates; battle metadata, rewards, priors and
        damage calibration are retained. Repeating this import is idempotent.
        """
        if server_namespace is None and "::" in battle_id:
            server_namespace, battle_id = battle_id.rsplit("::", 1)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            clause = "(battle_id=? OR json_extract(report_json,'$.battle_tag')=?)"
            values = [battle_id, battle_id]
            if server_namespace is not None:
                clause += " AND server_namespace=?"
                values.append(_namespace(server_namespace))
            row = db.execute("SELECT report_json FROM battles WHERE " + clause
                             + " ORDER BY ended_at DESC,rowid DESC LIMIT 1", values).fetchone()
            if row is None:
                return None
            report = json.loads(row["report_json"])
            clean = [_decision(item) for item in report.get("decisions", []) if isinstance(item, dict)]
            findings, updates = _progress_analysis(clean, bool(report.get("learning_enabled", False)))
            inserted = self._store_progress(db, report, updates)
            already_current = report.get("progress_analysis_version") == 1 and report.get("progress_updates") == updates
            if already_current:
                return {**report, "progress_imported": inserted, "progress_duplicate": True}
            self._backup_progress_artifacts(report)
            progress_kinds = {"setup_phazed", "setup_ko", "damaging_cycle", "failed_action"}
            report["findings"] = [item for item in report.get("findings", []) if item.get("kind") not in progress_kinds] + findings
            report["hypotheses"] = list(dict.fromkeys(item.get("hypothesis") for item in report["findings"] if item.get("hypothesis")))
            report["base_summary"] = report.get("base_summary", report.get("summary", ""))
            report["summary"] = report["base_summary"] + f" Progress: {len(updates)} counters or cycles observed."
            report["progress_analysis_version"], report["progress_updates"] = 1, updates
            report["schema_version"] = max(3, int(report.get("schema_version") or 1))
            report["limits"] = list(dict.fromkeys([*report.get("limits", []),
                "Progress evidence describes observed counters and cycles; it does not forbid an action when there is lasting gain or the conditions change."]))
            db.execute("UPDATE battles SET report_json=? WHERE server_namespace=? AND battle_id=?",
                       (json.dumps(report, ensure_ascii=False), report["server_namespace"], report["battle_id"]))
        self._refresh_progress_artifacts(report)
        return {**report, "progress_imported": inserted, "progress_duplicate": False}

    def _owned_artifact(self, raw_path: str | None) -> Path | None:
        if not raw_path:
            return None
        target = Path(raw_path).resolve()
        return target if target.is_relative_to(self.base_dir.resolve()) else None

    def _backup_progress_artifacts(self, report: dict) -> None:
        for key in ("analysis_path", "json_path"):
            path = self._owned_artifact(report.get(key))
            if path is not None and path.is_file():
                backup = path.with_name(path.name + ".pre-progress-v3.bak")
                if not backup.exists():
                    backup.write_bytes(path.read_bytes())

    def _refresh_progress_artifacts(self, report: dict) -> None:
        try:
            for key, content in (("analysis_path", self._markdown(report)),
                                 ("json_path", json.dumps(report, ensure_ascii=False, indent=2))):
                path = self._owned_artifact(report.get(key))
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._write_atomic(path, content)
        except OSError:
            report["file_warning"] = "Progress updated in SQLite; an analysis file could not be updated."

    def record_battle(self, record: dict, decisions: list[dict], learning_enabled: bool = True) -> dict:
        battle_id = _text(record.get("battle_id"), 200)
        format_id, team_id = _text(record.get("format_id"), 80), _text(record.get("team_id"), 128)
        if not battle_id or not format_id or not team_id:
            raise ValueError("battle_id, format_id and team_id are required to keep experiences apart.")
        result = record.get("result")
        if result not in {"win", "loss", "tie"}:
            raise ValueError("Result must be win, loss or tie.")
        namespace = _namespace(record.get("server_namespace", record.get("server", "showdown")))
        metadata = {
            "battle_id": battle_id, "battle_tag": _text(record.get("battle_tag", battle_id), 200),
            "storage_id": f"{namespace}::{battle_id}", "server_namespace": namespace,
            "ended_at": _text(record.get("ended_at") or datetime.now(timezone.utc).isoformat(), 80),
            "format_id": format_id, "team_id": team_id, "username": _text(record.get("username"), 80),
            "opponent": _text(record.get("opponent"), 80), "result": result,
            "turns": max(0, int(_number(record.get("turns")) or 0)),
            "rating_before": _number(record.get("rating_before")), "rating_after": _number(record.get("rating_after")),
            "opponent_rating": _number(record.get("opponent_rating")),
            "opponent_pokemon": [_text(name, 80) for name in (record.get("opponent_pokemon") or [])[:6]],
            "replay_path": _text(record.get("replay_path"), 1000) or None,
        }
        clean = [_decision(item) for item in decisions[:5000] if isinstance(item, dict)]
        move_priors = _observed_moves(clean) if learning_enabled else set()
        forecast_updates = []
        policy_updates = []
        findings, progress_updates = _progress_analysis(clean, learning_enabled)
        updates = []
        for index, decision in enumerate(clean):
            if learning_enabled:
                policy_updates.extend({"decision_index": index, **sample} for sample in _policy_samples(decision))
            items, reward, reason = _inspect(decision, clean[:index])
            calibrated = _calibration_sample(decision) if learning_enabled else None
            if calibrated is not None and decision.get("context_key") and decision.get("action_key"):
                forecast_updates.append({"decision_index": index, "context_key": decision["context_key"],
                                         "action_key": decision["action_key"], **calibrated})
            findings.extend(items)
            decision["local_evaluation"] = {"reward": reward, "reason": reason, "learned": False}
            if learning_enabled and reward is not None and decision.get("context_key") and decision.get("action_key"):
                updates.append({"decision_index": index, "turn": decision.get("turn", 0),
                                "context_key": _text(decision["context_key"], 1200),
                                "action_key": _text(decision["action_key"], 200), "reward": reward, "reason": reason})
                decision["local_evaluation"]["learned"] = True
        result_name = {"win": "Win", "loss": "Loss", "tie": "Tie"}[result]
        summary = f"{result_name} in {metadata['turns']} turns. {len(clean)} decisions recorded, {len(findings)} observations and {len(updates)} local learning signals."
        if not learning_enabled:
            summary += " Learning off; history and analysis only."
        file_id = hashlib.sha256(f"{namespace}\0{battle_id}".encode()).hexdigest()[:24]
        analysis_dir = self.base_dir / "analyses"
        report = {
            "schema_version": 4, **metadata, "summary": summary, "learning_enabled": bool(learning_enabled),
            "analysis_path": str((analysis_dir / f"{file_id}.md").resolve()),
            "json_path": str((analysis_dir / f"{file_id}.json").resolve()),
            "findings": findings, "hypotheses": list(dict.fromkeys(item["hypothesis"] for item in findings if item["hypothesis"])),
            "decisions": clean, "learning_updates": updates,
            "observed_move_updates": [{"species": species, "move": move} for species, move in sorted(move_priors)],
            "forecast_updates": forecast_updates,
            "policy_updates": policy_updates,
            "progress_analysis_version": 1, "progress_updates": progress_updates,
            "limits": ["Deterministic analysis based on the protocol and the available estimates.",
                       "A loss does not make every decision wrong; no global penalty is applied.",
                       "Hindsight does not prove which alternative would have won, nor guarantee fixing every loss.",
                       f"Reward adjustments need three comparable battles, are kept per format and team and are capped at ±{MAX_ADJUSTMENT:g} points.",
                       "Directly observed counters can be remembered after one battle; cycles only count when there was no lasting gain. The current decision re-checks these conditions.",
                       "Revealed moves update the opponent priors; prediction errors are kept in the calibration history for diagnosis.",
                       "Local signals can miss the later payoff of sacks and long plans; they are correlations, not causal proof."],
        }
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT report_json FROM battles WHERE server_namespace=? AND battle_id=?", (namespace, battle_id)).fetchone()
            if existing:
                existing_report = json.loads(existing[0])
                existing_report["duplicate"] = True
                return existing_report
            db.execute("INSERT INTO battles VALUES (?,?,?,?,?,?,?,?,?,?)", (namespace, battle_id, metadata["ended_at"], format_id, team_id,
                       metadata["username"], metadata["opponent"], result, metadata["turns"], json.dumps(report, ensure_ascii=False)))
            self._store_progress(db, metadata, progress_updates)
            if learning_enabled:
                self._store_matchups(db, namespace, battle_id, format_id, team_id, _matchup_samples(clean), result == "win")
            for sample in policy_updates:
                db.execute("INSERT INTO policy_evidence VALUES (?,?,?,?,?,?,?,?,?)",
                           (namespace, battle_id, sample["decision_index"], format_id, team_id,
                            sample["context_key"], sample["feature"], sample["residual"], sample["exposure"]))
            if policy_updates:
                db.execute("DELETE FROM policy_evidence WHERE rowid NOT IN (SELECT rowid FROM policy_evidence ORDER BY rowid DESC LIMIT ?)", (MAX_TOTAL_SAMPLES,))
            for item in updates:
                db.execute("INSERT INTO action_evidence(server_namespace,battle_id,decision_index,format_id,team_id,context_key,action_key,reward,reason) VALUES (?,?,?,?,?,?,?,?,?)",
                           (namespace, battle_id, item["decision_index"], format_id, team_id, item["context_key"], item["action_key"], item["reward"], item["reason"]))
                db.execute("DELETE FROM action_evidence WHERE format_id=? AND team_id=? AND context_key=? AND action_key=? AND id NOT IN (SELECT id FROM action_evidence WHERE format_id=? AND team_id=? AND context_key=? AND action_key=? ORDER BY id DESC LIMIT ?)",
                           (format_id, team_id, item["context_key"], item["action_key"], format_id, team_id, item["context_key"], item["action_key"], MAX_CONTEXT_SAMPLES))
            for species, move in move_priors:
                db.execute("INSERT OR IGNORE INTO observed_moves VALUES (?,?,?,?,?)", (namespace, battle_id, format_id, species, move))
            for sample in forecast_updates:
                db.execute("INSERT INTO forecast_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (namespace, battle_id, sample["decision_index"], format_id, team_id, sample["context_key"],
                            sample["action_key"], sample["observed"], sample["expected"], sample["error"], int(sample["outside_range"])))
            if updates:
                db.execute("DELETE FROM action_evidence WHERE id NOT IN (SELECT id FROM action_evidence ORDER BY id DESC LIMIT ?)", (MAX_TOTAL_SAMPLES,))
            if move_priors:
                db.execute("DELETE FROM observed_moves WHERE rowid NOT IN (SELECT rowid FROM observed_moves ORDER BY rowid DESC LIMIT ?)", (MAX_TOTAL_SAMPLES,))
            if forecast_updates:
                db.execute("DELETE FROM forecast_evidence WHERE rowid NOT IN (SELECT rowid FROM forecast_evidence ORDER BY rowid DESC LIMIT ?)", (MAX_TOTAL_SAMPLES,))
        try:
            analysis_dir.mkdir(parents=True, exist_ok=True)
            self._write_atomic(Path(report["analysis_path"]), self._markdown(report))
            self._write_atomic(Path(report["json_path"]), json.dumps(report, ensure_ascii=False, indent=2))
        except OSError as exc:
            report["file_warning"] = f"History saved in SQLite; not every analysis file could be written: {type(exc).__name__}."
            report["analysis_path"] = None
            report["json_path"] = None
            with self._connect() as db:
                db.execute("UPDATE battles SET report_json=? WHERE server_namespace=? AND battle_id=?", (json.dumps(report, ensure_ascii=False), namespace, battle_id))
        return report

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(text)
            temporary.replace(path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _markdown(report: dict) -> str:
        lines = [f"# Battle analysis {report['battle_tag']}", "", report["summary"], "",
                 f"Format: {report['format_id']} • Player: {report['username']} • Opponent: {report['opponent']}",
                 f"Ended: {report['ended_at']} • Server: {report['server_namespace']}", "",
                 "## Observations", ""]
        if not report["findings"]:
            lines.append("Not enough local evidence to point at a specific problem. That does not mean every choice was good.")
        for item in report["findings"]:
            lines.extend([f"### Turn {item['turn']}: {item['title']}", "", f"Action: {item['action']}. {item['detail']}", ""])
            if item["hypothesis"]:
                lines.extend([f"Hypothesis: {item['hypothesis']}", ""])
        lines.extend(["## Contextual memory", ""])
        if report["learning_updates"]:
            lines.extend(f"- Turn {item['turn']}: {item['reason']}" for item in report["learning_updates"])
        else:
            lines.append("No memory adjustment was recorded in this battle.")
        if report.get("progress_updates"):
            lines.extend(["", "## Observed counters and cycles", ""])
            for item in report["progress_updates"]:
                suffix = "Conditional evidence kept in memory." if item.get("learned") else "Learning off; analysis only."
                lines.append(f"- Turn {item['turn']}: {item['reason']} {suffix}")
        lines.extend(["", "## Limits of this analysis", ""])
        lines.extend(f"- {limit}" for limit in report["limits"])
        return "\n".join(lines) + "\n"

    def history(self, limit: int = 100) -> list[dict]:
        limit = min(5000, max(1, int(limit)))
        with self._connect() as db:
            rows = db.execute("SELECT s.summary_json FROM battles b JOIN battle_summary s "
                              "ON s.server_namespace=b.server_namespace AND s.battle_id=b.battle_id WHERE NOT EXISTS "
                              "(SELECT 1 FROM history_archive h WHERE h.server_namespace=b.server_namespace "
                              "AND h.battle_id=b.battle_id) ORDER BY b.ended_at DESC, b.rowid DESC LIMIT ?", (limit,)).fetchall()
        return [{key: summary.get(key) for key in HISTORY_KEYS} for summary in (json.loads(row[0]) for row in rows)]

    def clear_history(self) -> int:
        """Reset the visible list/counters, retaining independent learning evidence.

        Archive exact identities atomically: late ratings or repeated result
        events cannot resurrect cleared rows, and future matches stay visible.
        Replays and reports remain available on disk without touching teams.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return db.execute("INSERT OR IGNORE INTO history_archive(server_namespace,battle_id) "
                              "SELECT server_namespace,battle_id FROM battles").rowcount

    def get_report(self, battle_id: str, server_namespace: str | None = None) -> dict | None:
        if server_namespace is None and "::" in battle_id:
            server_namespace, battle_id = battle_id.rsplit("::", 1)
        with self._connect() as db:
            if server_namespace is None:
                row = db.execute("SELECT report_json FROM battles WHERE battle_id=? ORDER BY ended_at DESC, rowid DESC LIMIT 1", (battle_id,)).fetchone()
            else:
                row = db.execute("SELECT report_json FROM battles WHERE server_namespace=? AND battle_id=?", (_namespace(server_namespace), battle_id)).fetchone()
        return json.loads(row[0]) if row else None

    def update_rating(self, battle_id: str, server_namespace: str, before: float | None, after: float | None) -> dict | None:
        """Attach delayed ladder ratings without creating evidence or matches."""
        namespace = _namespace(server_namespace)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT report_json FROM battles WHERE server_namespace=? AND battle_id=?", (namespace, battle_id)).fetchone()
            if row is None:
                return None
            report = json.loads(row[0])
            for key, value in (("rating_before", before), ("rating_after", after)):
                numeric = _number(value)
                if numeric is not None:
                    report[key] = numeric
            db.execute("UPDATE battles SET report_json=? WHERE server_namespace=? AND battle_id=?", (json.dumps(report, ensure_ascii=False), namespace, battle_id))
        try:
            if report.get("analysis_path"):
                self._write_atomic(Path(report["analysis_path"]), self._markdown(report))
            if report.get("json_path"):
                self._write_atomic(Path(report["json_path"]), json.dumps(report, ensure_ascii=False, indent=2))
        except OSError:
            # SQLite remains authoritative if an editor temporarily locks a file.
            report["file_warning"] = "Rating updated in the history; the analysis file could not be updated."
        return report

    def reset_learning(self, format_id: str | None = None, team_id: str | None = None) -> int:
        """Explicit UI action only: erase memory while retaining match history."""
        clauses, parameters = [], []
        for key, value in (("format_id", format_id), ("team_id", team_id)):
            if value is not None:
                clauses.append(f"{key}=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as db:
            removed = db.execute("DELETE FROM action_evidence" + where, parameters).rowcount
            db.execute("DELETE FROM forecast_evidence" + where, parameters)
            removed += db.execute("DELETE FROM progress_evidence" + where, parameters).rowcount
            removed += db.execute("DELETE FROM policy_evidence" + where, parameters).rowcount
            db.execute("DELETE FROM matchup_evidence" + where, parameters)
            # Priors aggregate opponents across teams; a tier/global reset clears
            # them, while a team-only reset keeps unrelated opponent evidence.
            if team_id is None:
                db.execute("DELETE FROM observed_moves" + (" WHERE format_id=?" if format_id else ""),
                           (format_id,) if format_id else ())
            return removed
