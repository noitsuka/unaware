# Changelog

## 1.0.0

First public release.

- Foul Play MCTS over 32 sampled opponent worlds per decision, clock-aware thinking time, longer endgame search.
- Hybrid engine: a self-play value/policy network breaks the search's near-ties.
- Opponent scouting from public replays and past games, used as a prior on the sampled sets.
- Guards for no-op moves, loops without progress, Destiny Bond, choice locks into resists and guaranteed KOs.
- Search processes kept alive for the whole battle (no per-turn process start-up).
- Calibrated win chance, live field state, ladder rank of both players, Elo total per team.
- Desktop launcher: live battles, history with a short analysis of each game, team editor; reconnects to a battle in progress after a crash.
- Team library of 84 player-built teams (Smogon samples, tournament and ladder teams) across OU, Monotype, NU, RU, PU and LC, all checked by the official validator.
- Login with registered or unregistered Showdown names; the password is stored encrypted with Windows DPAPI.
