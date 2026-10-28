"""Live win probability: the network's p(win) averaged over sampled worlds.

Foul Play samples complete opponent sets (moves, items, Tera) consistent with
what the battle revealed; each sample becomes a poke-engine state, the value
head scores it from our side, and the mean is shown in the launcher. Like
Jaxcalibur's handling of hidden information (it averages over 32 worlds).

Weights: data/neural/ckpt.pt when you train locally (reloaded whenever
training writes a newer one), else the bundled weights.pt. Without torch or
the patched engine the estimate is simply absent.
"""
from __future__ import annotations

import threading
from pathlib import Path

LOCAL = Path(__file__).resolve().parents[2] / "data" / "neural" / "ckpt.pt"
BUNDLED = Path(__file__).with_name("weights.pt")
_lock = threading.Lock()
_cache: dict = {"mtime": None, "net": None}


def _net():
    path = LOCAL if LOCAL.is_file() else BUNDLED
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache["mtime"] != mtime:
        import torch

        from .model import PolicyValueNet
        net = PolicyValueNet()
        net.load_state_dict(torch.load(path, map_location="cpu")["net"])
        net.eval()
        _cache.update(mtime=mtime, net=net)
    return _cache["net"]


def win_probability(states: list[str]) -> float | None:
    """Mean p(win) for side one (us) over poke-engine state strings."""
    if not states:
        return None
    with _lock:
        try:
            import torch
            from poke_engine.poke_engine import Env
            net = _net()
            if net is None:
                return None
            env = Env(states)
            ints, mons, glob, order = [], [], [], []
            for index in range(len(states)):
                i, m, g = env.observe(index, 1)
                ints.append(i); mons.append(m); glob.append(g); order.append(env.switch_order(index, 1))
            torch.set_num_threads(1)  # never compete with the search
            with torch.no_grad():
                _, value = net(torch.tensor(ints), torch.tensor(mons, dtype=torch.float32).view(len(states), 12, 48),
                               torch.tensor(glob, dtype=torch.float32), torch.tensor(order))
            return float(torch.sigmoid(value).mean())
        except Exception:
            return None
