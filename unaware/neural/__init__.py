"""Neural bot in the style of Jaxcalibur (jaxcalibur.github.io), for gen9 OU.

Jaxcalibur (#1 on the gen9 random battle ladder, Aug 2026) is a transformer
trained by self-play PPO on a GPU battle engine written in JAX, combined with a
regret-matching pUCT search at test time. Its code and weights are not public,
and it plays random battles, not OU; this package rebuilds the approach.

The environment is poke-engine (the Rust simulator Foul Play uses), extended
with a batched self-play Env (vendor/poke-engine-patch): ~190k turns/s per core
with random moves, so the network, not the simulator, is the bottleneck.
"""
