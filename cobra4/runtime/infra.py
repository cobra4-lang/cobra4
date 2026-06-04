"""Declarative infrastructure runtime — `resource X = adapter { ... }`.

The cobra4 codegen turns each ``resource`` block into a
:func:`declare_resource` call. Resources are not applied by ``c4 run``;
the user invokes ``c4 infra plan|apply|destroy FILE`` to execute the
lifecycle. State persists between runs in
``./.cobra4/state.json`` (override with ``--state-file``).

Adapters implement four methods (``plan``, ``apply``, ``destroy``,
``read``) and register themselves under a dotted path:

.. code-block:: python

    @register_adapter("aws.s3")
    class S3Adapter:
        def plan(self, current, desired): ...
        def apply(self, current, desired): ...
        def destroy(self, current): ...

The shipped MVP includes the ``local.file`` adapter — writes JSON files
to disk — useful for demos and as a worked example for plugin authors.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

# ---------- public types ----------


@dataclass
class Action:
    kind: str  # "create" | "update" | "noop" | "destroy"
    diff: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # key -> (old, new)
    notes: str = ""


class InfraError(Exception):
    pass


class Resource:
    """A declared piece of infrastructure.

    The ``state`` dict is populated by the adapter's ``apply`` (or
    ``read``) and exposed as attribute access — so a downstream
    resource can do ``other_resource.endpoint`` in its field block.
    """

    __slots__ = ("name", "adapter_path", "fields_fn", "state")

    def __init__(
        self,
        name: str,
        adapter_path: str,
        fields_fn: Callable[[], dict[str, Any]],
    ) -> None:
        self.name = name
        self.adapter_path = adapter_path
        self.fields_fn = fields_fn
        self.state: dict[str, Any] = {}

    def __getattr__(self, key: str) -> Any:
        # __slots__ attrs are handled normally; this fires only for
        # state dict keys, allowing `resource.field` syntax in cobra4.
        try:
            return self.state[key]
        except KeyError as e:
            raise AttributeError(
                f"Resource {self.name!r} has no field {key!r} "
                f"(available: {sorted(self.state)})"
            ) from e

    def __repr__(self) -> str:
        return f"Resource({self.name!r}, adapter={self.adapter_path!r}, state={self.state!r})"


# ---------- adapter registry ----------


_ADAPTERS: dict[str, Any] = {}


def register_adapter(path: str, adapter: Any) -> None:
    """Register an adapter under a dotted path (``aws.s3``, ``local.file``)."""
    _ADAPTERS[path] = adapter


def get_adapter(path: str) -> Any:
    if path not in _ADAPTERS:
        raise InfraError(
            f"Unknown infra adapter: {path!r}. Registered: {sorted(_ADAPTERS)}"
        )
    return _ADAPTERS[path]


# ---------- declaration registry ----------


_REGISTRY: list[Resource] = []


def declare_resource(
    name: str,
    adapter_path: str,
    fields_fn: Callable[[], dict[str, Any]],
) -> Resource:
    res = Resource(name=name, adapter_path=adapter_path, fields_fn=fields_fn)
    _REGISTRY.append(res)
    return res


def clear_registry() -> None:
    _REGISTRY.clear()


def all_resources() -> list[Resource]:
    return list(_REGISTRY)


# ---------- state backend ----------


def _default_state_path() -> Path:
    return Path(".cobra4") / "state.json"


def load_state(path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    p = path or _default_state_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def save_state(state: dict[str, dict[str, Any]], path: Optional[Path] = None) -> None:
    """Persist state to disk **atomically** — write to a temp file in
    the same directory (unique per-process/per-thread to avoid two
    concurrent writers colliding on the same tmp name), fsync, then
    ``os.replace()``. A crash mid-write leaves either the old state or
    the new state, never a half-written file (which would break every
    subsequent ``plan`` / ``apply`` until manually fixed)."""
    import threading

    p = path or _default_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, sort_keys=True)
    # Unique tmp suffix so two threads (or processes) writing the same
    # state file don't both rename the same tmp out from under each other.
    tag = f".{os.getpid()}.{threading.get_ident()}.tmp"
    tmp = p.with_suffix(p.suffix + tag)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # fsync not supported on some FS — best effort
    os.replace(tmp, p)


# ---------- phases ----------


def plan(state_path: Optional[Path] = None) -> list[tuple[str, Action]]:
    """Return ``[(resource_name, Action), ...]`` showing what apply would do.

    Resources are processed in declaration order. After computing each
    resource's plan, we eagerly populate its ``state`` with the desired
    fields — so a downstream resource that references e.g.
    ``upstream.path`` inside its own block can resolve that name even
    though no apply has happened yet.
    """
    state = load_state(state_path)
    out: list[tuple[str, Action]] = []
    for r in _REGISTRY:
        adapter = get_adapter(r.adapter_path)
        desired = r.fields_fn()
        current = state.get(r.name, {})
        out.append((r.name, adapter.plan(current, desired)))
        # Allow downstream `fields_fn` to read this resource's desired
        # values via attribute access — same mechanism as apply.
        r.state = dict(desired)
    return out


def apply(state_path: Optional[Path] = None) -> dict[str, dict[str, Any]]:
    """Run the lifecycle. Resources are applied in declaration order."""
    state = load_state(state_path)
    new_state = dict(state)
    for r in _REGISTRY:
        adapter = get_adapter(r.adapter_path)
        desired = r.fields_fn()  # may reference earlier r.state via __getattr__
        current = state.get(r.name, {})
        result = adapter.apply(current, desired) or {}
        r.state = result
        new_state[r.name] = result
    save_state(new_state, state_path)
    return new_state


def destroy(state_path: Optional[Path] = None) -> None:
    """Tear down all resources in reverse declaration order."""
    state = load_state(state_path)
    new_state = dict(state)
    for r in reversed(_REGISTRY):
        if r.name in state:
            adapter = get_adapter(r.adapter_path)
            adapter.destroy(state[r.name])
            new_state.pop(r.name, None)
    save_state(new_state, state_path)


# ---------- built-in adapter: local.file ----------


class _LocalFileAdapter:
    """Trivial adapter that materialises a ``contents`` dict / str / bytes
    to ``path`` on disk. Useful for tests and as a reference plugin."""

    def plan(self, current: dict, desired: dict) -> Action:
        path = desired.get("path")
        if not path:
            raise InfraError("local.file: required field 'path' is missing")
        new_contents = desired.get("contents", "")
        if not current:
            return Action(kind="create", notes=f"will write {path!r}")
        diff: dict[str, tuple[Any, Any]] = {}
        if current.get("path") != path:
            diff["path"] = (current.get("path"), path)
        if current.get("contents") != new_contents:
            diff["contents"] = (current.get("contents"), new_contents)
        if not diff:
            return Action(kind="noop", notes=f"{path!r} matches state")
        return Action(kind="update", diff=diff, notes=f"will rewrite {path!r}")

    def apply(self, current: dict, desired: dict) -> dict:
        path_str = desired["path"]
        contents = desired.get("contents", "")
        path = Path(path_str)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, (dict, list)):
            payload = json.dumps(contents, indent=2, sort_keys=True)
        elif isinstance(contents, bytes):
            path.write_bytes(contents)
            return {
                "path": path_str,  # preserve user-typed path verbatim
                "contents": list(contents),
                "size": path.stat().st_size,
            }
        else:
            payload = str(contents)
        path.write_text(payload)
        return {
            "path": path_str,  # preserve user-typed path verbatim
            "contents": contents,
            "size": path.stat().st_size,
        }

    def destroy(self, current: dict) -> None:
        path = current.get("path")
        if path and Path(path).exists():
            try:
                Path(path).unlink()
            except IsADirectoryError:
                shutil.rmtree(path)


register_adapter("local.file", _LocalFileAdapter())


# Side-effect imports: register `aws.*` and `k8s.*` adapters.
# Each lives in its own module so heavy deps (boto3, kubectl) stay
# lazy — adapters delegate to mocks when their backend isn't available.
from cobra4.runtime import infra_aws as _aws_adapters  # noqa: E402, F401
from cobra4.runtime import infra_k8s as _k8s_adapters  # noqa: E402, F401

__all__ = [
    "Resource",
    "Action",
    "InfraError",
    "register_adapter",
    "get_adapter",
    "declare_resource",
    "clear_registry",
    "all_resources",
    "load_state",
    "save_state",
    "plan",
    "apply",
    "destroy",
]
