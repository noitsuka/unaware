# poke-engine 0.0.48, patched

[poke-engine](https://github.com/pmariglia/poke-engine) is the Rust battle engine
Foul Play searches with. `poke-engine.patch` adds:

- **`Env`** (`poke_engine.poke_engine.Env`): battles kept in Rust for self-play
  training (`masks`, `step`, `observe_all`, `winner`, `reset`, `serialize`;
  action slots 0-3 moves, 4-7 Tera moves, 8-13 switches, 14 none). Used by
  `unaware/neural` and `tools/neural_train.py`.
- **Evaluation tweaks**: Stealth Rock damage scales with the Pokémon's Rock
  weakness, and remaining PP of recovery moves is worth something, so long
  games stop throwing away Roost/Recover PP.
- **`rm_c`** on `monte_carlo_tree_search` (regret matching instead of UCB1).
  Off by default; it did not beat UCB1 in testing and the app does not use it.

The prebuilt wheel for Windows / Python 3.12 is in `vendor/wheels`. To rebuild it
(Rust GNU toolchain + MinGW):

    pip download poke-engine==0.0.48 --no-binary :all: --no-deps
    tar xzf poke_engine-0.0.48.tar.gz && cd poke_engine-0.0.48
    patch -p1 < poke-engine.patch
    maturin build --release --target x86_64-pc-windows-gnu --no-default-features --features poke-engine/terastallization
    pip install --force-reinstall target/wheels/poke_engine-0.0.48-*.whl
