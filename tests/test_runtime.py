"""Runtime tests: smart dispatcher, IO, concurrency, observe."""

from __future__ import annotations

import io
import json
import os
import tempfile

import pytest

from cobra4.runtime.smart import (
    SmartFn,
    smart,
    make_smart,
    AmbiguousDispatch,
    NoHandler,
)
from cobra4.runtime.io import read, save
from cobra4.runtime.concurrency import parallel_for
from cobra4.runtime import observe


# ---------- SmartFn ----------


def test_smart_basic_register_and_dispatch():
    f = make_smart("f")
    f.register(lambda x: ("str", x), type=str)
    f.register(lambda x: ("int", x), type=int)
    assert f("hi") == ("str", "hi")
    assert f(7) == ("int", 7)


def test_smart_specificity_wins():
    f = make_smart("f")
    f.register(lambda x: "any-str", type=str)
    f.register(lambda x: "csv-only", type=str, ext="csv")
    assert f("foo.csv") == "csv-only"
    assert f("foo.txt") == "any-str"


def test_smart_ambiguity_raises():
    f = make_smart("f")
    f.register(lambda x: "a", type=str)
    f.register(lambda x: "b", type=str)
    with pytest.raises(AmbiguousDispatch):
        f("hello")


def test_smart_no_handler_raises_when_no_default():
    f = make_smart("f")
    with pytest.raises(NoHandler):
        f("anything")


def test_smart_uses_default_when_no_handler_matches():
    @smart
    def fallback(x):
        return f"fallback:{x}"

    assert fallback("hello") == "fallback:hello"

    fallback.register(lambda x: f"int-handler:{x}", type=int)
    assert fallback(42) == "int-handler:42"
    assert fallback("hi") == "fallback:hi"  # falls back


def test_smart_caches_resolution_when_no_custom():
    """Without `when=`, the dispatcher caches by (type, scheme, ext, mime)."""
    f = make_smart("f")
    calls = {"n": 0}

    class _Counter:
        def __init__(self): pass
        def __call__(self, x):
            calls["n"] += 1
            return ("str", x)

    h = _Counter()
    # Register without `when=` so caching kicks in.
    f.register(h, type=str)
    f("x"); f("y"); f("z")
    assert calls["n"] == 3  # handler still runs every call (cache caches resolution, not result)


def test_smart_custom_predicate_runs_on_every_dispatch():
    """With `when=`, the cache is bypassed — predicates are re-checked.

    This is intentional: two values with the same (type, scheme, ext)
    might match different handlers based on `when=`, so caching the
    handler by classification alone would be unsafe.
    """
    f = make_smart("f")
    seen_a, seen_b = [], []
    f.register(lambda v: ("A", v), type=str, when=lambda v: (seen_a.append(v) or v.startswith("a")))
    f.register(lambda v: ("B", v), type=str, when=lambda v: (seen_b.append(v) or v.startswith("b")))

    assert f("alpha") == ("A", "alpha")
    assert f("beta") == ("B", "beta")
    # Each call must have re-evaluated predicates (no false caching).
    assert "alpha" in seen_a and "beta" in seen_a  # A's predicate sees both
    assert "beta" in seen_b  # B's predicate runs for "beta"


# ---------- IO smart dispatch ----------


def test_read_local_csv_dispatches_correctly():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "data.csv")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("a,b\n1,2\n3,4\n")
        rows = read(p)
        assert rows == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]


def test_read_local_json_dispatches_correctly():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "data.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"a": 1}, fh)
        assert read(p) == {"a": 1}


def test_save_local_json_then_read():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "out.json")
        save({"x": 1}, p)
        assert read(p) == {"x": 1}


def test_save_local_csv_then_read():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "out.csv")
        save([{"a": "1", "b": "2"}], p)
        assert read(p) == [{"a": "1", "b": "2"}]


def test_save_local_txt_then_read():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "out.txt")
        save("hello world", p)
        assert read(p) == "hello world"


def test_user_can_extend_read():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "data.fake")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("custom-format-payload")
        # Register an ad-hoc handler
        read.register(lambda t, **_: f"<<{open(t).read()}>>", type=str, scheme="file", ext="fake")
        try:
            assert read(p) == "<<custom-format-payload>>"
        finally:
            # Cleanup so other tests don't see this handler
            read._handlers = [h for h in read._handlers if h.name != None and h.pred.ext != "fake"]
            read._cache.clear()


# ---------- Concurrency ----------


def test_parallel_for_returns_results_in_order():
    out = parallel_for([1, 2, 3, 4, 5], lambda x: x * x, workers=4)
    assert out == [1, 4, 9, 16, 25]


def test_parallel_for_empty_returns_empty():
    assert parallel_for([], lambda x: x, workers=4) == []


def test_parallel_for_workers_one_uses_no_pool():
    """workers=1 path must preserve plain stack frames."""
    out = parallel_for([1, 2, 3], lambda x: x + 1, workers=1)
    assert out == [2, 3, 4]


# ---------- observe ----------


def test_log_writes_structured_line():
    buf = io.StringIO()
    observe.set_stream(buf)
    try:
        observe.log("hello", n=3)
        line = buf.getvalue()
        assert "level=info" in line
        assert "msg=hello" in line
        assert "n=3" in line
    finally:
        observe.set_stream(__import__("sys").stderr)


def test_log_warn_level():
    buf = io.StringIO()
    observe.set_stream(buf)
    try:
        observe.log.warn("bad", code=400)
        assert "level=warn" in buf.getvalue()
    finally:
        observe.set_stream(__import__("sys").stderr)
