"""Opponent's position in the official top 500, cached, never fatal."""
import io
import json

from unaware import ladder_rank


def fake_ladder(monkeypatch, rows, fail=False):
    calls = []

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if fail:
            raise OSError("offline")
        return io.BytesIO(json.dumps({"toplist": rows}).encode())
    monkeypatch.setattr(ladder_rank.urllib.request, "urlopen", urlopen)
    ladder_rank._cache.clear()
    return calls


def test_rank_elo_and_absent_players(monkeypatch):
    calls = fake_ladder(monkeypatch, [{"userid": "first", "elo": 1970.8, "gxe": 88.2},
                                      {"userid": "secondplace", "elo": 1925.2, "gxe": 81.1}])
    assert ladder_rank.lookup("Second Place", "gen9ou") == {"rank": 2, "elo": 1925, "gxe": 81.1}
    assert ladder_rank.lookup("someone", "gen9ou") == {"rank": None}
    assert len(calls) == 1  # cached


def test_offline_ladder_shows_nothing(monkeypatch):
    fake_ladder(monkeypatch, [], fail=True)
    assert ladder_rank.lookup("anyone", "gen9ou") == {}
