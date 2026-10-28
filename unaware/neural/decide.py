"""The network's move preferences over Foul Play's sampled worlds.

Foul Play keeps tracking the battle and sampling the opponent's possible sets;
each sampled world becomes a poke-engine state, the policy head scores our 15
action slots there, and the probabilities are averaged with the sample weights
(the "worlds" of Jaxcalibur). Names are in Foul Play's own format, so they can
replace or rank Foul Play's choices directly.

Used by the hybrid engine, where the network only breaks Foul Play's
near-ties (foulplay_brain).
"""
from __future__ import annotations

import logging

from .winprob import _lock, _net

logger = logging.getLogger(__name__)


def net_policy(battle, worlds: int = 8) -> dict[str, float]:
    """{"earthquake": .41, "switch gliscor": .2, ...} for moves legal in every world;
    {} with no network, at team preview or on any failure."""
    if getattr(battle, "team_preview", False):
        return {}
    try:
        from copy import deepcopy

        import numpy as np
        import torch
        from fp.search.poke_engine_helpers import battle_to_poke_engine_state
        from poke_engine.poke_engine import Env

        world = deepcopy(battle)
        samples = world.mode.prepare_battles(world, worlds)
        states = [battle_to_poke_engine_state(b).to_string() for b, _ in samples]
        weights = np.asarray([max(float(chance), 0.0) for _, chance in samples]) + 1e-9
        with _lock:
            net = _net()
            if net is None:
                return {}
            env = Env(states)
            n = len(states)
            ints, mons, glob, order, mask = env.observe_all()
            rows = slice(0, 2 * n, 2)  # our side is side one in every world
            ints = torch.from_numpy(np.frombuffer(ints, dtype=np.int32).reshape(2 * n, 122)[rows].astype(np.int64))
            mons = torch.from_numpy(np.frombuffer(mons, dtype=np.float32).reshape(2 * n, 12, 48)[rows].copy())
            glob = torch.from_numpy(np.frombuffer(glob, dtype=np.float32).reshape(2 * n, 40)[rows].copy())
            order = torch.from_numpy(np.frombuffer(order, dtype=np.int64).reshape(2 * n, 6)[rows].copy())
            legal = torch.from_numpy(np.frombuffer(mask, dtype=np.uint8).reshape(2 * n, 15)[rows].astype(bool))
            torch.set_num_threads(2)
            with torch.no_grad():
                logits, _ = net(ints, mons, glob, order)
                probs = torch.softmax(logits.masked_fill(~legal, -1e9), -1).numpy()
        mean = (probs * weights[:, None]).sum(0) / weights.sum()
        everywhere = legal.numpy().all(0)
        names = dict(env.option_names(0, 1))
        return {names[slot]: float(mean[slot]) for slot in names if everywhere[slot]}
    except Exception:
        logger.warning("network unavailable for this move", exc_info=True)
        return {}

