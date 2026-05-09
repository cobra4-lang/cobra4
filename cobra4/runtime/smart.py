"""Smart dispatch — the heart of cobra4's "do what I mean" mentality.

A ``SmartFn`` wraps a default callable plus a chain of *handlers*. Each
handler declares preconditions on the arguments (type, URI scheme, file
extension, MIME content-type, custom predicate) and a priority. When the
``SmartFn`` is called, the first handler whose preconditions match — at the
highest priority — wins.

Open dispatch: any library can register additional handlers on an existing
``SmartFn``. This is how cobra4 functions like :func:`cobra4.runtime.io.read`
acquire support for new formats and storage backends.

Decidability: ties at the same priority raise :class:`AmbiguousDispatch`
on the first call that triggers them. No silent fallback.

Performance: resolution caches by ``(type, scheme, ext)`` so repeated
calls amortize to O(1) lookup.
"""

from __future__ import annotations

import inspect
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlparse


_TRACE_ENABLED = os.environ.get("COBRA4_TRACE_DISPATCH") in ("1", "true", "yes")


def _trace_dispatch(fn_name: str, handler: Optional["Handler"], target: Any, kind: str) -> None:
    """Print a dispatch decision when ``COBRA4_TRACE_DISPATCH=1``.

    Lets users see which handler answered for which input — useful for
    debugging the "smart" routing without diving into the runtime.
    """
    if not _TRACE_ENABLED:
        return
    label = handler.name or handler.fn.__qualname__ if handler else "<default>"
    target_repr = repr(target)[:80]
    print(f"[c4-trace] {fn_name}({target_repr}) → {label} ({kind})", file=sys.stderr)


# ---------- Errors ----------


class AmbiguousDispatch(RuntimeError):
    """Raised when two handlers tie at the same priority for an input."""


class NoHandler(RuntimeError):
    """Raised when no handler matches and no default is set."""


# ---------- Predicates ----------


@dataclass
class Predicate:
    """Compiled predicate over a call's first positional argument.

    ``priority`` is the sum of the contributing constraints' specificity,
    so a handler that pins both ``scheme`` and ``ext`` outranks one that
    pins only ``scheme``.
    """

    type: Optional[type] = None
    scheme: Optional[str] = None
    ext: Optional[str] = None
    mime_prefix: Optional[str] = None
    custom: Optional[Callable[[Any], bool]] = None
    user_priority: int = 0

    def specificity(self) -> int:
        s = 0
        if self.type is not None:
            s += 4
        if self.scheme is not None:
            s += 4
        if self.ext is not None:
            s += 2
        if self.mime_prefix is not None:
            s += 2
        if self.custom is not None:
            s += 1
        return s + self.user_priority

    def matches(self, value: Any) -> bool:
        if self.custom is not None and not self.custom(value):
            return False
        if self.type is not None and not isinstance(value, self.type):
            return False
        scheme, ext, mime = _classify(value)
        if self.scheme is not None and scheme != self.scheme:
            return False
        if self.ext is not None and ext != self.ext:
            return False
        if self.mime_prefix is not None:
            if not mime or not mime.startswith(self.mime_prefix):
                return False
        return True


def _classify(value: Any) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract ``(scheme, ext, mime)`` from ``value`` for predicate matching.

    For strings it parses URI / path; for other types it is best-effort
    via attributes (``scheme``, ``ext``, ``mime``).
    """
    if isinstance(value, str):
        scheme, ext = _scheme_and_ext(value)
        return scheme, ext, None
    scheme = getattr(value, "scheme", None)
    ext = getattr(value, "ext", None)
    mime = getattr(value, "mime", None)
    return scheme, ext, mime


def _scheme_and_ext(s: str) -> tuple[Optional[str], Optional[str]]:
    # Windows drive letters like "C:\..." or "C:/..." are NOT URI schemes.
    if len(s) >= 2 and s[1] == ":" and (len(s) == 2 or s[2] in "\\/"):
        return "file", _ext_of_path(s)
    parsed = urlparse(s)
    scheme = parsed.scheme.lower() if parsed.scheme else None
    if scheme == "":
        scheme = None
    # Single-letter scheme is almost always a Windows drive letter; treat as file.
    if scheme is not None and len(scheme) == 1:
        return "file", _ext_of_path(s)
    # Local relative/absolute paths without a scheme → file.
    if scheme is None and (
        s.startswith("./") or s.startswith("../") or s.startswith("/")
        or s.startswith("\\") or _looks_like_local_path(s)
    ):
        scheme = "file"
    path = parsed.path or s
    return scheme, _ext_of_path(path)


def _ext_of_path(path: str) -> Optional[str]:
    last_segment = path.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in last_segment:
        return last_segment.rsplit(".", 1)[-1].lower()
    return None


def _looks_like_local_path(s: str) -> bool:
    if not s:
        return False
    # Heuristic: contains a dot extension and no `://`, doesn't start with
    # a scheme-y prefix.
    return "://" not in s and "." in s.rsplit("/", 1)[-1]


# ---------- Handlers ----------


@dataclass
class Handler:
    fn: Callable[..., Any]
    pred: Predicate
    name: Optional[str] = None

    def specificity(self) -> int:
        return self.pred.specificity()


# ---------- SmartFn ----------


@dataclass
class SmartFn:
    """An open, dispatch-driven callable.

    The original function (the one decorated with ``@smart``) acts as a
    fallback when no handler matches, unless an explicit default is set.

    The resolution cache is automatically bypassed when any registered
    handler uses a ``when=`` custom predicate — those depend on the
    actual value, not just its (type, scheme, ext, mime) classification.
    """

    name: str
    default: Optional[Callable[..., Any]] = None
    _handlers: list[Handler] = field(default_factory=list)
    _cache: dict[tuple, Handler] = field(default_factory=dict)
    _has_custom: Optional[bool] = None  # cached "any handler uses `when`?"
    # Optional effect required to invoke this SmartFn. Checked at call
    # time against the active sandbox (if any). ``None`` = no check.
    required_effect: Optional[str] = None

    @property
    def handlers(self) -> list[Handler]:
        """Read-only snapshot of registered handlers (highest specificity first)."""
        return sorted(self._handlers, key=lambda h: h.specificity(), reverse=True)

    def register(
        self,
        fn: Optional[Callable[..., Any]] = None,
        *,
        type: Optional[type] = None,  # noqa: A002 - mirroring user-facing API
        scheme: Optional[str] = None,
        ext: Optional[str] = None,
        mime: Optional[str] = None,
        when: Optional[Callable[[Any], bool]] = None,
        priority: int = 0,
        name: Optional[str] = None,
    ):
        """Register a handler. Usable as a decorator or by direct call.

        Examples::

            read.register(scheme="s3", ext="csv", fn=load_s3_csv)

            @save.register(type=DataFrame)
            def _(df, target): ...
        """

        def _do_register(handler_fn: Callable[..., Any]) -> Callable[..., Any]:
            pred = Predicate(
                type=type,
                scheme=scheme,
                ext=ext,
                mime_prefix=mime,
                custom=when,
                user_priority=priority,
            )
            self._handlers.append(Handler(fn=handler_fn, pred=pred, name=name))
            self._cache.clear()
            self._has_custom = None  # invalidate
            return handler_fn

        if fn is not None:
            return _do_register(fn)
        return _do_register

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        if self.required_effect is not None:
            from cobra4.runtime.effects import check as _check_effect
            _check_effect(self.required_effect)
        if not args:
            if self.default is not None:
                return self.default(*args, **kwargs)
            raise NoHandler(f"smart fn '{self.name}' requires at least one positional arg")
        target = args[0]
        # If ANY registered handler has a custom predicate, the cache is
        # unsafe (two values with the same (type, scheme, ext, mime) key
        # might match different handlers depending on `when`). In that
        # case we always re-resolve.
        has_custom = self._has_custom
        if has_custom is None:
            has_custom = any(h.pred.custom is not None for h in self._handlers)
            self._has_custom = has_custom

        if not has_custom:
            cache_key = _cache_key(target)
            cached = self._cache.get(cache_key)
            if cached is not None:
                _trace_dispatch(self.name, cached, target, "cached")
                return cached.fn(*args, **kwargs)
        else:
            cache_key = None

        handler = self._resolve(target)
        if handler is None:
            if self.default is not None:
                _trace_dispatch(self.name, None, target, "default")
                return self.default(*args, **kwargs)
            raise NoHandler(
                f"no handler for '{self.name}' matching argument of type "
                f"{type(target).__name__} (key={_cache_key(target)})"
            )
        if not has_custom and cache_key is not None:
            self._cache[cache_key] = handler
        _trace_dispatch(self.name, handler, target, "resolved")
        return handler.fn(*args, **kwargs)

    def _resolve(self, target: Any) -> Optional[Handler]:
        matching = [h for h in self._handlers if h.pred.matches(target)]
        if not matching:
            return None
        matching.sort(key=lambda h: h.specificity(), reverse=True)
        top = matching[0]
        ties = [h for h in matching if h.specificity() == top.specificity()]
        if len(ties) > 1:
            names = ", ".join(h.name or h.fn.__qualname__ for h in ties)
            raise AmbiguousDispatch(
                f"smart fn '{self.name}': multiple handlers tie at specificity "
                f"{top.specificity()} for {type(target).__name__} ({names})"
            )
        return top


def _cache_key(target: Any) -> tuple:
    scheme, ext, mime = _classify(target)
    return (type(target), scheme, ext, mime)


# ---------- Decorator ----------


def smart(fn: Callable[..., Any]) -> SmartFn:
    """Decorator: wrap ``fn`` as a :class:`SmartFn` with ``fn`` as default.

    The ``fn`` becomes the fallback used when no registered handler matches.
    """
    return SmartFn(name=fn.__name__, default=fn)


def make_smart(name: str, default: Optional[Callable[..., Any]] = None) -> SmartFn:
    """Create a SmartFn programmatically (used by stdlib at boot)."""
    return SmartFn(name=name, default=default)


# Re-export so ``inspect`` is referenced (silences linters that flag unused imports).
_ = inspect
