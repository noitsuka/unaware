"""A battle ending while the launcher reads history must still be saved."""
import sqlite3
from contextlib import closing

from unaware.learning import ExperienceStore
from test_learning import battle, decision


def test_battle_is_saved_while_history_is_being_read(tmp_path):
    store = ExperienceStore(tmp_path)
    store.record_battle(battle(0), [decision()])
    with closing(sqlite3.connect(store.path, isolation_level=None)) as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM battles").fetchone()[0] == 1
        store.record_battle(battle(1), [decision()])
        reader.execute("COMMIT")
    assert len(store.history()) == 2
