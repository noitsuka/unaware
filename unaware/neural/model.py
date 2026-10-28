"""Policy/value transformer over poke-engine observations (Jaxcalibur-style, CPU-sized).

Tokens: 12 Pokémon (own team active-first, then the foe's) and 1 field token.
Heads: 15 action logits (moves 0-3, Tera moves 4-7 from our active token; switches
8-13 from our team tokens; "none" 14 from the field token) and p(win).
Jaxcalibur uses 8.5M parameters on an H100; this one is ~1M for a 6-core CPU.
"""
from __future__ import annotations

import torch
from torch import nn

INTS_PER_MON, FLOATS_PER_MON, GLOBAL_FLOATS = 10, 48, 40
VOCAB = {"species": 2048, "type": 32, "item": 1024, "ability": 512, "move": 1024, "weather": 16, "terrain": 16}
ACTIONS = 15


class PolicyValueNet(nn.Module):
    def __init__(self, d: int = 128, layers: int = 3, heads: int = 4):
        super().__init__()
        self.species = nn.Embedding(VOCAB["species"], d)
        self.type_emb = nn.Embedding(VOCAB["type"], d)
        self.item = nn.Embedding(VOCAB["item"], d)
        self.ability = nn.Embedding(VOCAB["ability"], d)
        self.move = nn.Embedding(VOCAB["move"], d)
        self.weather = nn.Embedding(VOCAB["weather"], d)
        self.terrain = nn.Embedding(VOCAB["terrain"], d)
        self.mon_floats = nn.Linear(FLOATS_PER_MON, d)
        self.glob_floats = nn.Linear(GLOBAL_FLOATS, d)
        self.side = nn.Embedding(2, d)  # own / foe
        block = nn.TransformerEncoderLayer(d, heads, 2 * d, dropout=0.0, batch_first=True, norm_first=True)
        self.body = nn.TransformerEncoder(block, layers, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(d)
        self.move_head = nn.Linear(2 * d, 2)  # (move, Tera + move) per move slot
        self.move_key = nn.Linear(d, d)
        self.switch_head = nn.Linear(d, 1)
        self.none_head = nn.Linear(d, 1)
        self.value_head = nn.Sequential(nn.Linear(d, d), nn.GELU(), nn.Linear(d, 1))

    def forward(self, ints: torch.Tensor, mons: torch.Tensor, glob: torch.Tensor, switch_order: torch.Tensor):
        """ints (B, 122) long, mons (B, 12, 48), glob (B, 40), switch_order (B, 6) long.
        Returns logits (B, 15) and win probability logits (B,)."""
        batch = ints.shape[0]
        ids = ints[:, :120].view(batch, 12, INTS_PER_MON)
        moves = self.move(ids[:, :, 6:10])  # (B, 12, 4, d)
        tokens = (self.species(ids[:, :, 0]) + self.type_emb(ids[:, :, 1]) + self.type_emb(ids[:, :, 2])
                  + self.item(ids[:, :, 4]) + self.ability(ids[:, :, 5]) + moves.mean(2)
                  + self.mon_floats(mons))
        tera = self.type_emb(ids[:, :, 3])
        tokens = tokens + 0.5 * tera
        side = torch.cat([torch.zeros(6, dtype=torch.long), torch.ones(6, dtype=torch.long)]).to(ints.device)
        tokens = tokens + self.side(side)[None]
        field = self.glob_floats(glob) + self.weather(ints[:, 120]) + self.terrain(ints[:, 121])
        x = self.norm(self.body(torch.cat([tokens, field[:, None]], 1)))
        active, team, field_out = x[:, 0], x[:, :6], x[:, 12]
        # Each of our active's 4 moves: its embedding against the active token.
        move_in = torch.cat([self.move_key(moves[:, 0]), active[:, None].expand(-1, 4, -1)], -1)
        move_logits = self.move_head(move_in)  # (B, 4, 2)
        switch_logits = self.switch_head(team).squeeze(-1)  # (B, 6) in token order
        logits = torch.full((batch, ACTIONS), 0.0, device=ints.device)
        logits[:, 0:4] = move_logits[:, :, 0]
        logits[:, 4:8] = move_logits[:, :, 1]
        # Token k is team slot switch_order[k] in the engine's indexing.
        logits[:, 8:14] = torch.zeros(batch, 6, device=ints.device).scatter(1, switch_order, switch_logits)
        logits[:, 14] = self.none_head(field_out).squeeze(-1)
        return logits, self.value_head(field_out).squeeze(-1)
