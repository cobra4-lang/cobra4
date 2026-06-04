"""Result types for cobra4: ``Result[T, E]`` with ``Ok`` / ``Err`` variants
and the ``?`` postfix propagation operator.

A function that uses ``?`` is wrapped by the codegen with a
try/except for :class:`_C4Propagate`, so the early-return semantics
"just work" without the user writing any plumbing:

.. code-block:: cobra4

    fn parse_user(blob) -> Result[User, str] {
        name  = require_field(blob, "name")?     # if Err, propagate
        email = require_field(blob, "email")?
        return Ok(User(name, email))
    }

The unwrap helper :func:`_c4_try_propagate` raises
:class:`_C4Propagate` carrying the original ``Err`` instance — which the
function wrapper turns back into the function's return value, with the
correct type.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Generic, TypeVar

T = TypeVar("T")
E = TypeVar("E")


class Result(Generic[T, E]):
    """Marker base class. ``Ok(x)`` and ``Err(e)`` are subclasses."""

    __slots__ = ()

    def is_ok(self) -> bool:
        return isinstance(self, Ok)

    def is_err(self) -> bool:
        return isinstance(self, Err)


@dataclasses.dataclass(frozen=True)
class Ok(Result[T, E]):
    value: T

    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False


@dataclasses.dataclass(frozen=True)
class Err(Result[T, E]):
    error: E

    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True


class _C4Propagate(BaseException):
    """Internal sentinel raised by :func:`_c4_try_propagate` when an
    ``Err`` is encountered. Caught by the auto-generated function wrapper.

    Inherits from ``BaseException`` (not ``Exception``) so ordinary
    ``catch Exception`` clauses don't accidentally swallow it.
    """

    __slots__ = ("err",)

    def __init__(self, err: "Err") -> None:
        super().__init__()
        self.err = err


def _c4_try_propagate(value: Any) -> Any:
    """Implementation of the postfix ``?`` operator.

    - If ``value`` is :class:`Ok`, returns the unwrapped ``.value``.
    - If ``value`` is :class:`Err`, raises :class:`_C4Propagate` carrying it.
    - Otherwise raises :class:`TypeError` — ``?`` only makes sense on a
      ``Result``.
    """
    if isinstance(value, Ok):
        return value.value
    if isinstance(value, Err):
        raise _C4Propagate(value)
    raise TypeError(
        f"`?` operator expected a Result (Ok or Err), got {type(value).__name__}"
    )


__all__ = ["Result", "Ok", "Err", "_c4_try_propagate", "_C4Propagate"]
