"""Core runtime helpers used directly by transpiled cobra4 code.

These are deliberately tiny; bigger features live in dedicated modules
(io, concurrency, observe).

Includes stub registries for ``every``, ``on_event``, ``serve``, and
``deploy`` — in M1 they record callbacks rather than running a daemon.
The ``c4 serve`` command (M4) will pick up the registries.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------- ?. and ?? helpers ----------


def safe_attr(target: Any, name: str) -> Any:
    """``target?.name`` semantics.

    Resolution order:

    - ``None`` → ``None`` (safe-nav short-circuit).
    - dict / Mapping → ``target.get(name)`` so ``req?.params?.user_id``
      works when ``params`` is a JSON-decoded dict (the common case for
      web handlers, where attribute-style access on a dict would raise).
    - everything else → ``getattr(target, name, None)``. We choose
      ``None`` over ``AttributeError`` to match the safe-nav contract:
      a missing attribute composes with ``??`` cleanly.
    """
    if target is None:
        return None
    # Mapping path: handles dict, OrderedDict, defaultdict, ImmutableDict, etc.
    from collections.abc import Mapping

    if isinstance(target, Mapping):
        return target.get(name)
    return getattr(target, name, None)


def default(value: Any, fallback: Any) -> Any:
    """``value ?? fallback``: ``fallback`` only if ``value is None``."""
    return fallback if value is None else value


# ---------- every / on event registries ----------


@dataclass
class _ScheduleEntry:
    seconds: float
    fn: Callable[[], Any]


@dataclass
class _EventEntry:
    source: Any
    fn: Callable[[Any], Any]


_schedule_registry: list[_ScheduleEntry] = []
_event_registry: list[_EventEntry] = []


def every(seconds: float, fn: Callable[[], Any]) -> None:
    """Register a callback to be invoked every ``seconds``.

    In M1 this records the callback in :data:`_schedule_registry`.
    A daemon mode (``c4 serve``) wires up the actual loop in M4.
    """
    _schedule_registry.append(_ScheduleEntry(seconds=seconds, fn=fn))


def on_event(source: Any, fn: Callable[[Any], Any]) -> None:
    """Register a callback for events from ``source`` (e.g. a queue)."""
    _event_registry.append(_EventEntry(source=source, fn=fn))


def schedule_registry() -> list[_ScheduleEntry]:
    return list(_schedule_registry)


def event_registry() -> list[_EventEntry]:
    return list(_event_registry)


def run_scheduled_once() -> None:
    """Invoke each registered ``every`` callback exactly once.

    Useful for examples and tests in M1; the real daemon loop lands in M4.
    """
    for entry in _schedule_registry:
        entry.fn()


def run_scheduled_for(duration: float, *, sleep_resolution: float = 0.05) -> None:
    """Run all ``every`` callbacks for ``duration`` seconds, then return.

    Each entry tracks its own next-fire timestamp.
    """
    deadlines: dict[int, float] = {
        i: time.monotonic() for i in range(len(_schedule_registry))
    }
    end = time.monotonic() + duration
    while time.monotonic() < end:
        now = time.monotonic()
        for i, entry in enumerate(_schedule_registry):
            if now >= deadlines[i]:
                entry.fn()
                deadlines[i] = now + entry.seconds
        time.sleep(sleep_resolution)


# ---------- serve / deploy stubs ----------


@dataclass
class _ServeEntry:
    handler: Callable[..., Any]
    port: int


@dataclass
class _DeployEntry:
    handler: Callable[..., Any]
    target: Any
    body: Callable[[], Any] = field(default=lambda: None)


_serve_registry: list[_ServeEntry] = []
_deploy_registry: list[_DeployEntry] = []


def serve_handler(handler: Callable[..., Any], port: int) -> _ServeEntry:
    """Stub: register an HTTP handler. Real server in M4 (`c4 serve`)."""
    entry = _ServeEntry(handler=handler, port=port)
    _serve_registry.append(entry)
    return entry


def deploy_handler(
    handler: Callable[..., Any], target: Any, body: Callable[[], Any]
) -> _DeployEntry:
    """Stub: record a deployment intent. Real adapters in M3."""
    entry = _DeployEntry(handler=handler, target=target, body=body)
    _deploy_registry.append(entry)
    return entry


def serve_registry() -> list[_ServeEntry]:
    return list(_serve_registry)


def deploy_registry() -> list[_DeployEntry]:
    return list(_deploy_registry)


# ---------- testing helpers ----------


def reset_registries() -> None:
    """Clear all registries — used by tests to isolate runs."""
    _schedule_registry.clear()
    _event_registry.clear()
    _serve_registry.clear()
    _deploy_registry.clear()
