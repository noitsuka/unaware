from unaware.decision_clock import DecisionClock


def test_private_clock_sets_budget_with_transport_margin(monkeypatch):
    now = [100.]
    monkeypatch.setattr("unaware.decision_clock.time.monotonic", lambda: now[0])
    clock = DecisionClock()
    clock.observe("room", [["", "inactive", "Time left: 5 sec this turn ", " 100 sec total"]], "Bot")
    assert clock.budget("room", 7000) == 3000
    now[0] += 1
    assert clock.budget("room", 7000) == 2000
    now[0] += 3
    assert clock.budget("room", 7000) == 0


def test_opponents_clock_is_ignored_and_fresh_notice_restores_budget(monkeypatch):
    monkeypatch.setattr("unaware.decision_clock.time.monotonic", lambda: 100.)
    clock = DecisionClock()
    clock.observe("room", [["", "inactive", "Opponent has 1 seconds left."]], "Bot")
    assert clock.budget("room", 7000) == 7000
    clock.observe("room", [["", "inactive", "B-O-T has 4 seconds left this turn."]], "Bot")
    assert clock.budget("room", 7000) == 2000
    clock.observe("room", [["", "inactive", "Time left: 150 sec this turn"]], "Bot")
    assert clock.budget("room", 7000) == 7000
    clock.observe("room", [["", "inactiveoff", "Battle timer is now OFF."]], "Bot")
    assert not clock.deadlines


def test_room_cleanup_removes_only_its_timer():
    clock = DecisionClock()
    clock.observe("one", [["", "inactive", "Time left: 1 sec this turn"]], "Bot")
    clock.observe("two", [["", "inactive", "Time left: 2 sec this turn"]], "Bot")
    clock.clear("one")
    assert "one" not in clock.deadlines and "two" in clock.deadlines
