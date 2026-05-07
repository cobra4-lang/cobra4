"""M4 tests: scheduler / event loop / HTTP."""

from __future__ import annotations

import time
import urllib.request

from cobra4.runtime import core as core_rt
from cobra4.runtime import schedule
from cobra4.runtime.schedule import (
    InMemoryQueue,
    queue,
    serve_forever,
)


def test_in_memory_queue_put_poll():
    q = InMemoryQueue("orders")
    q.put({"id": 1})
    events = list(q.poll(timeout=0.1))
    assert events == [{"id": 1}]


def test_queue_factory_idempotent():
    a = queue("foo")
    b = queue("foo")
    assert a is b


def test_scheduler_runs_and_stops():
    """Verify that `every` callbacks fire when the daemon runs briefly."""
    core_rt.reset_registries()
    counter = {"n": 0}
    core_rt.every(0.05, lambda: counter.update(n=counter["n"] + 1))
    serve_forever(timeout=0.3)
    assert counter["n"] >= 2


def test_event_loop_dispatches_to_handler():
    core_rt.reset_registries()
    q = InMemoryQueue("events-test")
    received = []
    core_rt.on_event(q, lambda ev: received.append(ev))
    # populate before serve so first poll picks them up
    q.put("a")
    q.put("b")
    q.put("c")
    serve_forever(timeout=0.3)
    assert set(received) == {"a", "b", "c"}


def test_http_server_responds():
    core_rt.reset_registries()

    def handler(req):
        return {"hello": "cobra4"}

    core_rt.serve_handler(handler, port=18081)

    import threading

    t = threading.Thread(target=serve_forever, kwargs={"timeout": 1.0}, daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        resp = urllib.request.urlopen("http://127.0.0.1:18081/", timeout=2)
        body = resp.read().decode("utf-8")
        assert '"hello": "cobra4"' in body
    finally:
        t.join(timeout=2.0)
