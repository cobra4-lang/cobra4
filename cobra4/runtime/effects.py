"""Runtime effect / capability sandbox for cobra4.

The static effect system (``fn f() with [http]``) catches mistakes at
``c4 check`` time. The runtime sandbox catches them at runtime — useful
when the call chain crosses untrusted plugin code, or when a third
party can inject code through ``use`` / dynamic dispatch that the
static checker can't see.

Usage in cobra4:

.. code-block:: cobra4

    sandbox [http, log] {
        # Inside this block, only `http` and `log` effects are allowed.
        # Calling `read("./file")` (effect: fs) raises EffectViolation.
        fetch("https://api.example.com")
        log("ok")
    }

Implementation notes:

- Effect masks live in a ``threading.local()`` so concurrent threads
  (each with their own ``each ... in parallel``) don't bleed effects.
- Sandbox blocks nest: a child block's effect set must be a subset of
  the enclosing block's. This is what every capability-system theorist
  expects (you can't elevate by nesting).
- Outside a sandbox, no checks fire — keeping the runtime free for
  trusted modules. Only opt-in.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional


class EffectViolation(RuntimeError):
    """Raised when a builtin requiring an effect is invoked inside a
    sandbox that doesn't allow that effect."""


_state = threading.local()


def _stack() -> list[frozenset[str]]:
    if not hasattr(_state, "stack"):
        _state.stack = []
    return _state.stack


def current_allowed() -> Optional[frozenset[str]]:
    """The active effect mask, or ``None`` if no sandbox is currently
    in effect (and thus all effects are allowed)."""
    s = _stack()
    return s[-1] if s else None


def check(effect: str) -> None:
    """Verify that ``effect`` is allowed in the current scope. No-op
    outside any sandbox. Called at the top of every effectful builtin."""
    cur = current_allowed()
    if cur is not None and effect not in cur:
        raise EffectViolation(
            f"effect {effect!r} is not in the active sandbox "
            f"({sorted(cur)} allowed)"
        )


@contextmanager
def with_effects(*allowed: str) -> Iterator[frozenset[str]]:
    """Context manager pushing an allowed-effect mask onto the stack.

    Nesting rule: the new mask is intersected with the outer one — you
    cannot elevate by nesting (sandbox semantics). If you write

        with with_effects("http"):
            with with_effects("http", "fs"):
                ...

    the inner block's *effective* mask is just ``{"http"}``."""
    new_set = frozenset(allowed)
    outer = current_allowed()
    if outer is not None:
        new_set = new_set & outer
    s = _stack()
    s.append(new_set)
    try:
        yield new_set
    finally:
        s.pop()


__all__ = ["EffectViolation", "current_allowed", "check", "with_effects"]
