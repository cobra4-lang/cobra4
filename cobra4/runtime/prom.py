"""Prometheus metrics runtime for cobra4.

The ``prom`` language plugin (``lang use prom``) compiles ``metric``
declarations into calls to :func:`make_counter` /
:func:`make_histogram` / :func:`make_gauge` exported here.

Implementation:
- If ``prometheus_client`` is installed, we delegate to it (real
  process-wide metrics, ready for /metrics scrape).
- Otherwise we ship a tiny built-in implementation that supports the
  same surface (``inc()``, ``observe()``, ``set()``, ``labels()``)
  so the demo works offline and tests don't pull a heavy dep.

Both implementations expose the same :func:`metrics_text` function
that returns a Prometheus text-format string for the /metrics
endpoint.
"""

from __future__ import annotations

import threading
from typing import Any, Optional


# ---------- prometheus_client backend (preferred) ----------


def _try_use_prometheus_client():
    try:
        import prometheus_client as pc  # type: ignore
    except ImportError:
        return None
    return pc


_PC = _try_use_prometheus_client()


# ---------- built-in fallback ----------


class _Metric:
    """Common base for the in-process fallback."""

    __slots__ = ("name", "doc", "label_names", "_data", "_lock")

    def __init__(self, name: str, doc: str, label_names: list[str]) -> None:
        self.name = name
        self.doc = doc
        self.label_names = list(label_names)
        # key: tuple of label values in `label_names` order; value: float / list
        self._data: dict[tuple, Any] = {}
        self._lock = threading.Lock()

    def _key_from_labels(self, **labels: Any) -> tuple:
        if set(labels) != set(self.label_names):
            raise ValueError(
                f"metric {self.name!r} expects labels {self.label_names}, "
                f"got {sorted(labels)}"
            )
        return tuple(labels[n] for n in self.label_names)


class _Counter(_Metric):
    def labels(self, **labels: Any) -> "_CounterChild":
        return _CounterChild(self, self._key_from_labels(**labels))

    def inc(self, amount: float = 1.0) -> None:
        if self.label_names:
            raise ValueError(
                f"counter {self.name!r} requires .labels(...) — has {self.label_names}"
            )
        with self._lock:
            self._data[()] = self._data.get((), 0.0) + amount


class _CounterChild:
    __slots__ = ("_parent", "_key")

    def __init__(self, parent: _Counter, key: tuple) -> None:
        self._parent = parent
        self._key = key

    def inc(self, amount: float = 1.0) -> None:
        with self._parent._lock:
            self._parent._data[self._key] = self._parent._data.get(self._key, 0.0) + amount


class _Histogram(_Metric):
    __slots__ = ("buckets",)

    def __init__(
        self, name: str, doc: str, label_names: list[str], buckets: list[float],
    ) -> None:
        super().__init__(name, doc, label_names)
        self.buckets = sorted(buckets) + [float("inf")]

    def labels(self, **labels: Any) -> "_HistogramChild":
        return _HistogramChild(self, self._key_from_labels(**labels))

    def observe(self, value: float) -> None:
        if self.label_names:
            raise ValueError(
                f"histogram {self.name!r} requires .labels(...) — has {self.label_names}"
            )
        self._observe((), value)

    def _observe(self, key: tuple, value: float) -> None:
        with self._lock:
            entry = self._data.setdefault(key, {"count": 0, "sum": 0.0, "buckets": [0] * len(self.buckets)})
            entry["count"] += 1
            entry["sum"] += value
            for i, ub in enumerate(self.buckets):
                if value <= ub:
                    entry["buckets"][i] += 1


class _HistogramChild:
    __slots__ = ("_parent", "_key")

    def __init__(self, parent: _Histogram, key: tuple) -> None:
        self._parent = parent
        self._key = key

    def observe(self, value: float) -> None:
        self._parent._observe(self._key, value)


class _Gauge(_Metric):
    def labels(self, **labels: Any) -> "_GaugeChild":
        return _GaugeChild(self, self._key_from_labels(**labels))

    def set(self, value: float) -> None:
        if self.label_names:
            raise ValueError(
                f"gauge {self.name!r} requires .labels(...) — has {self.label_names}"
            )
        with self._lock:
            self._data[()] = float(value)

    def inc(self, amount: float = 1.0) -> None:
        if self.label_names:
            raise ValueError(f"gauge {self.name!r} requires .labels(...) for inc()")
        with self._lock:
            self._data[()] = self._data.get((), 0.0) + amount


class _GaugeChild:
    __slots__ = ("_parent", "_key")

    def __init__(self, parent: _Gauge, key: tuple) -> None:
        self._parent = parent
        self._key = key

    def set(self, value: float) -> None:
        with self._parent._lock:
            self._parent._data[self._key] = float(value)

    def inc(self, amount: float = 1.0) -> None:
        with self._parent._lock:
            self._parent._data[self._key] = self._parent._data.get(self._key, 0.0) + amount


# ---------- registry ----------


_REGISTRY: list[Any] = []


def _register(m: Any) -> Any:
    _REGISTRY.append(m)
    return m


# ---------- factory functions (the surface the plugin compiles to) ----------


def make_counter(name: str, *, labels: Optional[list[str]] = None, doc: str = "") -> Any:
    if _PC is not None:
        m = _PC.Counter(name, doc, labels or [])
    else:
        m = _Counter(name, doc, labels or [])
    return _register(m)


def make_histogram(
    name: str,
    *,
    labels: Optional[list[str]] = None,
    buckets: Optional[list[float]] = None,
    doc: str = "",
) -> Any:
    default_buckets = [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
    if _PC is not None:
        m = _PC.Histogram(name, doc, labelnames=labels or [], buckets=tuple(buckets or default_buckets))
    else:
        m = _Histogram(name, doc, labels or [], buckets or default_buckets)
    return _register(m)


def make_gauge(name: str, *, labels: Optional[list[str]] = None, doc: str = "") -> Any:
    if _PC is not None:
        m = _PC.Gauge(name, doc, labels or [])
    else:
        m = _Gauge(name, doc, labels or [])
    return _register(m)


def reset_registry() -> None:
    """Test helper. Not exposed in the language."""
    _REGISTRY.clear()


# ---------- text exposition ----------


def metrics_text() -> str:
    """Return a Prometheus text-format string for the /metrics endpoint."""
    if _PC is not None:
        return _PC.generate_latest().decode("utf-8")

    out: list[str] = []
    for m in _REGISTRY:
        kind = (
            "counter" if isinstance(m, _Counter) else
            "histogram" if isinstance(m, _Histogram) else
            "gauge" if isinstance(m, _Gauge) else "untyped"
        )
        if m.doc:
            out.append(f"# HELP {m.name} {m.doc}")
        out.append(f"# TYPE {m.name} {kind}")
        if isinstance(m, _Histogram):
            for key, entry in m._data.items():
                lstr = "{" + ",".join(f'{n}="{v}"' for n, v in zip(m.label_names, key)) + "}" if m.label_names else ""
                for ub, c in zip(m.buckets, entry["buckets"]):
                    if m.label_names:
                        out.append(f"{m.name}_bucket{{{lstr.strip('{}')}{',' if lstr.strip('{}') else ''}le=\"{ub}\"}} {c}")
                    else:
                        out.append(f'{m.name}_bucket{{le="{ub}"}} {c}')
                out.append(f"{m.name}_sum{lstr} {entry['sum']}")
                out.append(f"{m.name}_count{lstr} {entry['count']}")
        else:
            for key, value in m._data.items():
                if m.label_names:
                    lstr = "{" + ",".join(f'{n}="{v}"' for n, v in zip(m.label_names, key)) + "}"
                else:
                    lstr = ""
                out.append(f"{m.name}{lstr} {value}")
    return "\n".join(out) + "\n"


__all__ = [
    "make_counter", "make_histogram", "make_gauge",
    "metrics_text", "reset_registry",
]
