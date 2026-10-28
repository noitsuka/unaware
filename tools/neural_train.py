"""Self-play PPO for the neural bot (Jaxcalibur-style), on poke-engine.

    .venv/Scripts/python tools/neural_train.py --hours 8

Runs at Windows IDLE priority: it only uses CPU nothing else wants, so the
ladder bot keeps its full search time. Checkpoints and the CSV log go to
data/neural/ (not in git). Every --eval-every updates the network plays 200
games against random moves and 200 against a greedy "strongest move" player.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import random
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from poke_engine.poke_engine import Env  # noqa: E402

from unaware.neural.model import ACTIONS, PolicyValueNet  # noqa: E402
from unaware.neural.teams import load_teams, team_state  # noqa: E402

OUT = ROOT / "data" / "neural"
MAX_TURNS = 400


def idle_priority():
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x40)


class Games:
    """N self-play battles with random OU team pairs."""

    def __init__(self, n: int, seed: int):
        self.rng = random.Random(seed)
        self.teams = list(load_teams().values())
        self.env = Env([self.start() for _ in range(n)], seed=seed)
        self.turns = [0] * n

    def start(self) -> str:
        return team_state(self.rng.choice(self.teams), self.rng.choice(self.teams), self.rng)

    def observe_all(self):
        """Both perspectives of every battle (env i side 1 = row 2i, side 2 = row 2i+1),
        built in Rust in one call."""
        ints, mons, glob, order, mask = self.env.observe_all()
        n = 2 * len(self.turns)
        return (torch.from_numpy(np.frombuffer(ints, dtype=np.int32).reshape(n, 122).astype(np.int64)),
                torch.from_numpy(np.frombuffer(mons, dtype=np.float32).reshape(n, 12, 48).copy()),
                torch.from_numpy(np.frombuffer(glob, dtype=np.float32).reshape(n, 40).copy()),
                torch.from_numpy(np.frombuffer(order, dtype=np.int64).reshape(n, 6).copy()),
                torch.from_numpy(np.frombuffer(mask, dtype=np.uint8).reshape(n, 15).astype(bool)))

    def observe(self, players):
        """players: list of (env index, perspective). Tensors for the batch + masks."""
        ints, mons, glob, order, mask = [], [], [], [], []
        for index, side in players:
            i, m, g = self.env.observe(index, side)
            ints.append(i); mons.append(m); glob.append(g)
            order.append(self.env.switch_order(index, side))
            m1, m2 = self.env.masks(index)
            mask.append(m1 if side == 1 else m2)
        return (torch.tensor(ints, dtype=torch.long), torch.tensor(mons, dtype=torch.float32).view(len(players), 12, 48),
                torch.tensor(glob, dtype=torch.float32), torch.tensor(order, dtype=torch.long), torch.tensor(mask))


def masked(logits, mask):
    return logits.masked_fill(~mask, -1e9)


def greedy_action(mons_row, mask_row):
    """Strongest legal move by base power x accuracy (our active is token 0)."""
    best, score = None, 0.0
    for j in range(4):
        if mask_row[j]:
            power = mons_row[0, 24 + 6 * j] * mons_row[0, 25 + 6 * j]
            if power > score:
                best, score = j, power
    if best is None:
        legal = [k for k in range(ACTIONS) if mask_row[k]]
        best = random.choice(legal)
    return best


@torch.no_grad()
def evaluate(net, opponent: str, games: int = 200, seed: int = 99) -> float:
    pool = Games(32, seed)
    wins = finished = 0
    while finished < games:
        ints, mons, glob, order, mask = pool.observe_all()
        logits, _ = net(ints, mons, glob, order)
        net_actions = torch.distributions.Categorical(logits=masked(logits, mask)).sample()
        for i in range(32):
            a1 = int(net_actions[2 * i])
            m2 = mask[2 * i + 1]
            if opponent == "random":
                a2 = random.choice([k for k in range(ACTIONS) if m2[k]])
            elif opponent.startswith("mcts"):
                a2 = pool.env.mcts_action(i, 2, int(opponent[4:] or 1000))
            else:
                a2 = greedy_action(mons[2 * i + 1], m2)
            result = pool.env.step(i, a1, a2)
            pool.turns[i] += 1
            if result or pool.turns[i] >= MAX_TURNS:
                finished += 1
                wins += result > 0
                pool.env.reset(i, pool.start()); pool.turns[i] = 0
    return wins / finished


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--envs", type=int, default=128)
    ap.add_argument("--steps", type=int, default=64, help="turns per rollout")
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--eval-every", type=int, default=50)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--minibatch", type=int, default=4096)
    args = ap.parse_args()
    idle_priority()
    torch.set_num_threads(args.threads)
    OUT.mkdir(parents=True, exist_ok=True)
    net = PolicyValueNet()
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    update = games_done = 0
    if args.resume and (OUT / "ckpt.pt").exists():
        ck = torch.load(OUT / "ckpt.pt")
        net.load_state_dict(ck["net"]); opt.load_state_dict(ck["opt"])
        update, games_done = ck["update"], ck["games"]
    print(f"parameters: {sum(p.numel() for p in net.parameters()):,}", flush=True)
    games = Games(args.envs, seed=update + 1)
    players = [(i, s) for i in range(args.envs) for s in (1, 2)]
    log = open(OUT / "train_log.csv", "a", newline="")
    writer = csv.writer(log)
    began = time.time()
    while time.time() - began < args.hours * 3600:
        tick = time.time()
        buf = {k: [] for k in ("ints", "mons", "glob", "order", "mask", "act", "logp", "val", "rew", "done")}
        for _ in range(args.steps):
            ints, mons, glob, order, mask = games.observe_all()
            with torch.no_grad():
                logits, value = net(ints, mons, glob, order)
                dist = torch.distributions.Categorical(logits=masked(logits, mask))
                act = dist.sample()
            rew = torch.zeros(len(players)); done = torch.zeros(len(players))
            results = games.env.step_all(act.tolist())
            for i in range(args.envs):
                result = results[i]
                games.turns[i] += 1
                if result or games.turns[i] >= MAX_TURNS:
                    # 1 for a win, 0 for a loss, 0.5 if cut at MAX_TURNS.
                    rew[2 * i] = 1.0 if result > 0 else 0.0 if result < 0 else .5
                    rew[2 * i + 1] = 1.0 - rew[2 * i]
                    done[2 * i] = done[2 * i + 1] = 1
                    games.env.reset(i, games.start()); games.turns[i] = 0; games_done += 1
            for k, v in (("ints", ints), ("mons", mons), ("glob", glob), ("order", order), ("mask", mask), ("act", act),
                         ("logp", dist.log_prob(act)), ("val", torch.sigmoid(value)), ("rew", rew), ("done", done)):
                buf[k].append(v)
        with torch.no_grad():
            _, last = net(*games.observe_all()[:4])
            last = torch.sigmoid(last)
        # GAE (gamma 1, lambda .95) per player slot.
        T = args.steps
        adv = torch.zeros(T, len(players)); gae = torch.zeros(len(players))
        for t in reversed(range(T)):
            nxt = last if t == T - 1 else buf["val"][t + 1]
            live = 1 - buf["done"][t]
            delta = buf["rew"][t] + live * nxt - buf["val"][t]
            gae = delta + .95 * live * gae
            adv[t] = gae
        ret = (adv + torch.stack(buf["val"])).clamp(0, 1)
        flat = {k: torch.cat(v) if k in ("ints", "mons", "glob", "order", "mask") else torch.stack(v).flatten()
                for k, v in buf.items()}
        flat["adv"], flat["ret"] = adv.flatten(), ret.flatten()
        choice = flat["mask"].sum(1) > 1  # forced moves teach nothing
        idx = torch.nonzero(choice).squeeze(1)
        a = flat["adv"][idx]; flat["adv"][idx] = (a - a.mean()) / (a.std() + 1e-8)
        stats = []
        for _ in range(args.epochs):
            perm = idx[torch.randperm(len(idx))]
            for start in range(0, len(perm), args.minibatch):
                b = perm[start:start + args.minibatch]
                logits, value = net(flat["ints"][b], flat["mons"][b], flat["glob"][b], flat["order"][b])
                dist = torch.distributions.Categorical(logits=masked(logits, flat["mask"][b]))
                ratio = (dist.log_prob(flat["act"][b]) - flat["logp"][b]).exp()
                pg = -torch.min(ratio * flat["adv"][b], ratio.clamp(.8, 1.2) * flat["adv"][b]).mean()
                vloss = F.binary_cross_entropy_with_logits(value, flat["ret"][b])
                ent = dist.entropy().mean()
                loss = pg + .5 * vloss - .01 * ent
                opt.zero_grad(); loss.backward()
                torch.nn.utils.clip_grad_norm_(net.parameters(), .5)
                opt.step()
                stats.append((pg.item(), vloss.item(), ent.item()))
        update += 1
        pg, vl, en = np.mean(stats, 0)
        rate = args.steps * args.envs / (time.time() - tick)
        row = [update, games_done, round(rate), round(pg, 4), round(vl, 4), round(en, 3), "", "", ""]
        if update % args.eval_every == 0:
            net.eval()
            row[6], row[7] = round(evaluate(net, "random"), 3), round(evaluate(net, "greedy"), 3)
            # poke-engine's MCTS with full information, 1000 iterations a move.
            row[8] = round(evaluate(net, "mcts1000", games=64), 3)
            net.train()
            torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "update": update, "games": games_done}, OUT / "ckpt.pt")
        writer.writerow(row); log.flush()
        print(f"update {update} games {games_done} {rate:.0f} turns/s pg {pg:.3f} value {vl:.3f} entropy {en:.2f}"
              + (f" | vs aleatorio {row[6]:.0%} vs guloso {row[7]:.0%} vs MCTS {row[8]:.0%}" if row[6] != "" else ""), flush=True)
    torch.save({"net": net.state_dict(), "opt": opt.state_dict(), "update": update, "games": games_done}, OUT / "ckpt.pt")


if __name__ == "__main__":
    main()
