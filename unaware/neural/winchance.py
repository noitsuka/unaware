"""Live win chance shown on the battle card.

The network alone (trained on self-play with full information) was badly
calibrated on the ladder: 28-32% at 6x6, 50-70% predictions won 27% of the
time, jumps of 25 points between similar turns (6 sampled worlds), and frozen
values when Foul Play stopped. Now:

- material model: logistic regression on alive and HP differences, weighted
  more late in the game, fitted on our own analysed games (slopes on all 388,
  base level on the 50 games since Foul Play); out-of-sample Brier .189 vs .248
  for a constant, and "40-60%" won 53% of the time. Computed every turn from
  the battle, so it never freezes;
- the network (matchups: types, boosts, sets over Foul Play's worlds) moves it
  in logit space with weight NET_WEIGHT;
- a moving average keeps one turn from swinging the number.

15/10: games started at 38-39% even when won. The fitted intercept (-.30, 42%
at 6x6) is our old record, not the position: set to 0, so even material is
50%. The network read ~33% at preview whatever the matchup: that is its bias.
Its first estimate of every battle is averaged (START_FILE) and subtracted, so
it moves the number by how much better or worse this matchup looks than a
typical one. Its worlds are Foul Play's sampled sets, which carry the scouting
prior (moves/items this player showed get ~75% of the draws, fp_scouting), so
what scouting knows about the opponent is in the number from the preview on.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

MODEL = Path(__file__).with_name("material_model.json")
START_FILE = Path(__file__).resolve().parents[2] / "data" / "neural" / "winchance_start.json"
START_MIN_GAMES = 5
START_SEED = .32  # read back from the 15/10 games that showed 38-39% at preview
NET_WEIGHT = .4
SMOOTHING = .6  # weight of the new estimate against the previous one
_weights: list[float] | None = None


def _logit(p: float) -> float:
    p = min(max(p, 1e-3), 1 - 1e-3)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def material_features(own_alive, foe_alive, own_hp, foe_hp) -> list[float]:
    late = (12 - own_alive - foe_alive) / 12
    return [1.0, own_alive - foe_alive, own_hp - foe_hp, (own_alive - foe_alive) * late, (own_hp - foe_hp) * late]


def material_probability(own_alive, foe_alive, own_hp, foe_hp) -> float:
    global _weights
    if _weights is None:
        _weights = json.loads(MODEL.read_text(encoding="utf-8"))["weights"]
    return _sigmoid(sum(w * x for w, x in zip(_weights, material_features(own_alive, foe_alive, own_hp, foe_hp))))


def battle_material(battle) -> tuple[float, float, float, float]:
    """Alive counts and HP sums (fractions); unrevealed foes count as alive at full HP."""
    team = list(battle.team.values()) or list(getattr(battle, "teampreview_team", []))
    own_alive = sum(1 for mon in team if not mon.fainted) if battle.team else 6
    own_hp = sum(mon.current_hp_fraction for mon in team if not mon.fainted) if battle.team else 6.0
    size = len(getattr(battle, "teampreview_opponent_team", []) or []) or 6
    seen = list(battle.opponent_team.values())
    hidden = max(0, size - len(seen))
    foe_alive = sum(1 for mon in seen if not mon.fainted) + hidden
    # Team preview lists the foe's six with HP 0 (unknown), and they keep it
    # until they come in: read as full HP, not as fainted (that showed 96-99%
    # at preview and inflated the chance while foes stayed unrevealed).
    foe_hp = sum(_known_hp(mon) for mon in seen if not mon.fainted) + hidden
    return own_alive, foe_alive, own_hp, foe_hp


def _known_hp(mon) -> float:
    return mon.current_hp_fraction if getattr(mon, "current_hp", 0) or getattr(mon, "max_hp", 0) else 1.0


def start_bias() -> float:
    """The network's average first estimate over past battles (START_SEED until START_MIN_GAMES)."""
    try:
        data = json.loads(START_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return START_SEED
    return _sigmoid(data["mean_logit"]) if data.get("games", 0) >= START_MIN_GAMES else START_SEED


def note_start(net: float) -> None:
    """Fold one battle's first network estimate into the average (running mean)."""
    try:
        data = json.loads(START_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {"games": 0, "mean_logit": 0.}
    games = data.get("games", 0) + 1
    data = {"games": games, "mean_logit": data.get("mean_logit", 0.) + (_logit(net) - data.get("mean_logit", 0.)) / min(games, 200)}
    try:
        START_FILE.parent.mkdir(parents=True, exist_ok=True)
        START_FILE.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def combine(material: float, net: float | None, previous: float | None, baseline: float | None = None) -> float:
    shift = 0. if net is None else _logit(net) - (_logit(baseline) if baseline is not None else 0.)
    estimate = _sigmoid(_logit(material) + NET_WEIGHT * shift)
    return estimate if previous is None else SMOOTHING * estimate + (1 - SMOOTHING) * previous
