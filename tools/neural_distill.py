"""Expert iteration: the network learns to imitate poke-engine's MCTS.

    .venv/Scripts/python tools/neural_distill.py generate --rows 1000000
    .venv/Scripts/python tools/neural_distill.py train --epochs 4

generate: MCTS plays both sides (visits sampled for variety in the first turns,
sharpened later); every decision stores the observation, the search's visit
share over the 15 action slots and, at the end, who won. Worker processes run at
Windows IDLE priority. train: cross-entropy to the visit share + value loss on
the result, then the usual evaluation (random, greedy, MCTS). The checkpoint
(data/neural/ckpt.pt) feeds the live win chance and PPO fine-tuning
(tools/neural_train.py --resume).
"""
from __future__ import annotations

import argparse
import glob
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
OUT = ROOT / "data" / "neural"
SHARDS = OUT / "distill"


def worker(index: int, rows: int, iterations: int, envs: int, keep: float = 1.0, tag: str = "w"):
    import neural_train as nt
    nt.idle_priority()
    rng = random.Random(1000 + index)
    games = nt.Games(envs, seed=1000 + index)
    pending = [[] for _ in range(envs)]  # per env: (row arrays..., side)
    buf = {k: [] for k in ("ints", "mons", "glob", "order", "mask", "policy", "value")}
    made, shard = 0, 0
    while made < rows:
        ints, mons, glob_, order, mask = games.env.observe_all()
        n = 2 * envs
        ints = np.frombuffer(ints, dtype=np.int32).reshape(n, 122)
        mons = np.frombuffer(mons, dtype=np.float32).reshape(n, 12, 48)
        glob_ = np.frombuffer(glob_, dtype=np.float32).reshape(n, 40)
        order = np.frombuffer(order, dtype=np.int64).reshape(n, 6)
        mask = np.frombuffer(mask, dtype=np.uint8).reshape(n, 15)
        actions = []
        for i in range(envs):
            p1, p2 = games.env.mcts_policy(i, iterations)
            for side, policy in ((0, p1), (1, p2)):
                row = 2 * i + side
                # Positions of one game are alike and share its result: keeping only
                # part of them gives the value head more games for the same rows.
                if mask[row].sum() > 1 and rng.random() < keep:
                    pending[i].append((ints[row].astype(np.int16), mons[row].astype(np.float16), glob_[row].astype(np.float16),
                                       order[row].astype(np.int8), mask[row].copy(), np.asarray(policy, np.float16), side))
                weights = np.asarray(policy, np.float64) ** (1 if games.turns[i] < 8 else 3)
                if weights.sum() <= 0:
                    weights = mask[row].astype(np.float64)
                actions.append(rng.choices(range(15), weights=weights)[0])
        results = games.env.step_all(actions)
        for i, result in enumerate(results):
            games.turns[i] += 1
            if result or games.turns[i] >= nt.MAX_TURNS:
                z = (1.0 if result > 0 else 0.0) if result else .5
                for *arrays, side in pending[i]:
                    for key, value in zip(("ints", "mons", "glob", "order", "mask", "policy"), arrays):
                        buf[key].append(value)
                    buf["value"].append(np.float16(z if side == 0 else 1 - z))
                made += len(pending[i])
                pending[i] = []
                games.env.reset(i, games.start()); games.turns[i] = 0
        if len(buf["value"]) >= 20000 or (made >= rows and buf["value"]):
            np.savez(SHARDS / f"{tag}{index}_{shard:04d}.npz", **{k: np.stack(v) for k, v in buf.items()})
            shard += 1
            buf = {k: [] for k in buf}
            print(f"worker {index}: {made} decisions", flush=True)


def generate(args):
    SHARDS.mkdir(parents=True, exist_ok=True)
    per = args.rows // args.workers
    procs = [mp.Process(target=worker, args=(w, per, args.iterations, args.envs, args.keep, args.tag))
             for w in range(args.workers)]
    began = time.time()
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    print(f"generated in {time.time() - began:.0f}s", flush=True)


def load(paths):
    parts = [np.load(p) for p in paths]
    return {k: np.concatenate([p[k] for p in parts]) for k in parts[0].files}


def train(args):
    import torch
    import torch.nn.functional as F

    import neural_train as nt
    from unaware.neural.model import PolicyValueNet
    nt.idle_priority()
    torch.set_num_threads(args.threads)
    paths = sorted(glob.glob(str(SHARDS / "*.npz")))
    random.Random(0).shuffle(paths)
    held = max(1, len(paths) // 20)
    val, data = load(paths[:held]), load(paths[held:])
    print(f"train {len(data['value'])} decisions, validation {len(val['value'])}", flush=True)
    net = PolicyValueNet()
    ckpt = OUT / "ckpt.pt"
    if args.resume and ckpt.exists():
        net.load_state_dict(torch.load(ckpt)["net"])
    opt = torch.optim.AdamW(net.parameters(), lr=args.lr, weight_decay=1e-4)

    def batch(d, idx):
        return (torch.from_numpy(d["ints"][idx].astype(np.int64)), torch.from_numpy(d["mons"][idx].astype(np.float32)),
                torch.from_numpy(d["glob"][idx].astype(np.float32)), torch.from_numpy(d["order"][idx].astype(np.int64)),
                torch.from_numpy(d["mask"][idx].astype(bool)), torch.from_numpy(d["policy"][idx].astype(np.float32)),
                torch.from_numpy(d["value"][idx].astype(np.float32)))

    def losses(d, idx):
        ints, mons, glob_, order, mask, policy, value = batch(d, idx)
        logits, v = net(ints, mons, glob_, order)
        logp = F.log_softmax(logits.masked_fill(~mask, -1e9), -1)
        pl = -(policy * logp).sum(-1).mean()
        vl = F.binary_cross_entropy_with_logits(v, value)
        agree = (logits.masked_fill(~mask, -1e9).argmax(-1) == policy.argmax(-1)).float().mean()
        return pl, vl, agree

    n = len(data["value"])
    steps = args.epochs * (n // args.batch)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=args.lr, total_steps=max(1, steps))
    step, best, best_state = 0, float("inf"), None
    for epoch in range(args.epochs):
        perm = np.random.permutation(n)
        net.train()
        for start in range(0, n - args.batch + 1, args.batch):
            pl, vl, _ = losses(data, perm[start:start + args.batch])
            opt.zero_grad(); (pl + args.value_weight * vl).backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step(); sched.step(); step += 1
        net.eval()
        with torch.no_grad():
            idx = np.arange(min(len(val["value"]), 20000))
            pl, vl, agree = losses(val, idx)
        score = float(pl + vl)
        print(f"epoch {epoch + 1}: validation policy {pl:.3f} value {vl:.3f} MCTS move agreement {agree:.0%}", flush=True)
        if score < best:
            best = score
            best_state = {k: v.clone() for k, v in net.state_dict().items()}
            torch.save({"net": best_state, "opt": torch.optim.Adam(net.parameters()).state_dict(),
                        "update": 0, "games": 0}, ckpt)
            print("  checkpoint saved (best validation)", flush=True)
    net.load_state_dict(best_state)
    with torch.no_grad():
        for opponent, games in (("random", 200), ("greedy", 200), ("mcts200", 64), ("mcts1000", 64)):
            print(f"network vs {opponent}: {nt.evaluate(net, opponent, games=games):.0%}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--rows", type=int, default=1_000_000)
    g.add_argument("--workers", type=int, default=6)
    g.add_argument("--iterations", type=int, default=2000)
    g.add_argument("--envs", type=int, default=16)
    g.add_argument("--keep", type=float, default=1.0, help="share of positions stored per game")
    g.add_argument("--tag", default="w", help="shard file prefix")
    t = sub.add_parser("train")
    t.add_argument("--epochs", type=int, default=4)
    t.add_argument("--batch", type=int, default=1024)
    t.add_argument("--lr", type=float, default=1e-3)
    t.add_argument("--threads", type=int, default=6)
    t.add_argument("--resume", action="store_true")
    t.add_argument("--value-weight", type=float, default=.5)
    args = ap.parse_args()
    generate(args) if args.cmd == "generate" else train(args)


if __name__ == "__main__":
    main()
