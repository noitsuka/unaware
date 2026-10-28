"""Stopping must interrupt a stuck worker without waiting for its RPC timeout."""
import subprocess
import sys
import threading
import time

import pytest

from unaware.search import SearchUnavailable, SimulatorSearch


def test_abort_unblocks_inflight_rpc_and_prevents_restart(monkeypatch):
    original = subprocess.Popen
    processes = []

    def stalled_worker(*args, **kwargs):
        process = original([sys.executable, "-u", "-c",
            "import sys,time; print('{\"ready\": true}', flush=True); "
            "sys.stdin.readline(); time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr("unaware.search.subprocess.Popen", stalled_worker)
    worker = SimulatorSearch(node_path=sys.executable)
    failures = []

    def request():
        try:
            worker.healthcheck()
        except SearchUnavailable as error:
            failures.append(str(error))

    task = threading.Thread(target=request)
    try:
        task.start()
        deadline = time.monotonic() + 3
        while worker._process is None and time.monotonic() < deadline:
            time.sleep(.005)
        assert worker._process is not None
        began = time.monotonic()
        worker.abort()
        assert time.monotonic() - began < .3
        task.join(2)
        assert not task.is_alive()
        assert failures and "cancelled" in failures[0]
        with pytest.raises(SearchUnavailable, match="cancelled"):
            worker.healthcheck()
        assert len(processes) == 1
        processes[0].wait(timeout=2)
    finally:
        worker.close()
