"""Convert server timer notices into a conservative per-turn search budget."""
import re
import time


class DecisionClock:
    def __init__(self):
        self.deadlines = {}

    def observe(self, tag, messages, username):
        identity = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
        for message in messages:
            if len(message) < 3:
                continue
            kind, text = message[1], "|".join(message[2:])
            if kind == "inactiveoff" and "timer is now OFF" in text:
                self.deadlines.pop(tag, None)
                continue
            if kind != "inactive":
                continue
            private = re.match(r"Time left: (\d+) sec this turn", text)
            public = re.match(r"(.+?) has (\d+) seconds left(?: this turn)?\.", text)
            seconds = (int(private[1]) if private else int(public[2])
                       if public and identity(public[1]) == identity(username) else None)
            if seconds is not None:
                self.deadlines[tag] = time.monotonic() + seconds

    def budget(self, tag, maximum_ms):
        deadline = self.deadlines.get(tag)
        if deadline is None:
            return maximum_ms
        # Leave time for scoring, transport and the server's timer granularity.
        return max(0, min(maximum_ms, int((deadline - time.monotonic() - 2) * 1000)))

    def clear(self, tag):
        self.deadlines.pop(tag, None)
