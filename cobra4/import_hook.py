"""Generic ``.c4`` module import hook.

Lets ``import some_module`` (or ``use some_module`` in cobra4) resolve
``./some_module.c4`` from the current sys.path entries — not just files
under ``cobra4/stdlib``.

The hook:

1. Walks ``sys.path`` looking for ``<entry>/<NAME>.c4`` (or, for dotted
   names, the corresponding directory chain ``<entry>/A/B.c4`` for
   ``A.B``).
2. If found, transpiles and caches the bytecode under
   ``<dir>/__pycache__/<NAME>.cobra4.pyc`` (mtime-keyed).
3. Honors plugin directives (``lang use sql``, etc.) at the top of the
   file via the standard preprocess pipeline.

Activation: install once at process start by importing :mod:`cobra4` —
the package's ``__init__`` does this automatically.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import marshal
import os
import struct
import sys
import types
from pathlib import Path
from typing import Optional


_MAGIC = b"C4MOD\x00\x01\x02"
_HEADER_FMT = f"<{len(_MAGIC)}sQQ"


def _cache_path_for(c4_path: Path) -> Path:
    cache_dir = c4_path.parent / "__pycache__"
    try:
        cache_dir.mkdir(exist_ok=True)
    except OSError:
        pass
    return cache_dir / f"{c4_path.stem}.cobra4.pyc"


def _load_cached(c4_path: Path):
    cache = _cache_path_for(c4_path)
    if not cache.exists():
        return None
    try:
        st = c4_path.stat()
        with open(cache, "rb") as fh:
            header = fh.read(struct.calcsize(_HEADER_FMT))
            if len(header) != struct.calcsize(_HEADER_FMT):
                return None
            magic, mtime, size = struct.unpack(_HEADER_FMT, header)
            if magic != _MAGIC or mtime != st.st_mtime_ns or size != st.st_size:
                return None
            return marshal.loads(fh.read())
    except (OSError, ValueError, EOFError):
        return None


def _store_cached(c4_path: Path, code) -> None:
    cache = _cache_path_for(c4_path)
    try:
        st = c4_path.stat()
        with open(cache, "wb") as fh:
            fh.write(struct.pack(_HEADER_FMT, _MAGIC, st.st_mtime_ns, st.st_size))
            fh.write(marshal.dumps(code))
    except OSError:
        pass


def _compile_c4(path: Path) -> object:
    """Parse + lower + codegen, returning a Python code object."""
    cached = _load_cached(path)
    if cached is not None:
        return cached
    # Lazy imports to avoid circular dependencies.
    from cobra4.parser import parse
    from cobra4.lowering import lower
    from cobra4.codegen import generate
    from cobra4.plugins import preprocess

    src = path.read_text(encoding="utf-8")
    pre = preprocess(src)
    module = parse(pre.source, source_path=str(path))
    py_src = generate(lower(module), cobra4_path=str(path)).code
    if pre.plugins:
        plugin_imports = "\n".join(
            f"from {p.runtime_module} import *  # plugin: {p.name}"
            for p in pre.plugins if p.runtime_module
        )
        if plugin_imports:
            py_src = plugin_imports + "\n" + py_src
    code = compile(py_src, str(path), "exec")
    _store_cached(path, code)
    return code


class _C4Loader(importlib.abc.Loader):
    def __init__(self, fullname: str, path: Path) -> None:
        self.fullname = fullname
        self.path = path

    def create_module(self, spec):
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        code = _compile_c4(self.path)
        module.__file__ = str(self.path)
        module.__loader__ = self
        exec(code, module.__dict__)


class _C4Finder(importlib.abc.MetaPathFinder):
    """Locate ``foo`` or ``a.b`` modules as ``.c4`` files on ``sys.path``."""

    # Module names we explicitly DO NOT take over — these are owned by Python or
    # other finders. Without this, importing top-level ``json``/``os``/etc.
    # could be intercepted if the user accidentally has ``json.c4`` lying around.
    _BLOCKED = {"json", "os", "sys", "io", "re", "csv", "time", "math", "random"}

    def find_spec(self, fullname: str, path, target=None):
        if fullname in self._BLOCKED:
            return None
        # Resolve dotted name as a path under each sys.path entry.
        rel = fullname.replace(".", os.sep) + ".c4"
        search_dirs = list(path) if path else list(sys.path)
        for d in search_dirs:
            if not d:
                continue
            try:
                candidate = Path(d) / rel
                if candidate.is_file():
                    return importlib.machinery.ModuleSpec(
                        fullname,
                        _C4Loader(fullname, candidate),
                        origin=str(candidate),
                    )
            except (OSError, TypeError):
                continue
        return None


_installed = False


def install() -> None:
    """Install the .c4 finder. Idempotent."""
    global _installed
    if _installed:
        return
    if not any(isinstance(f, _C4Finder) for f in sys.meta_path):
        # Insert AFTER the stdlib loader so `cobra4.stdlib.X` (which has its
        # own dedicated finder) wins. Generic .c4 lookup is last-resort.
        sys.meta_path.append(_C4Finder())
    _installed = True


def uninstall() -> None:
    """Remove the finder (used by tests)."""
    global _installed
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _C4Finder)]
    _installed = False
