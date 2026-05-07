"""cobra4 stdlib — modules written in cobra4 itself.

A custom import hook lets ``import cobra4.stdlib.http`` (and similar)
load the corresponding ``.c4`` file directly, transpile it, and execute
the result.

Caching:
    The compiled bytecode is cached on disk under
    ``__pycache__/<name>.cobra4-<hash>.pyc`` next to the ``.c4`` source.
    The cache key is the source file's mtime+size. Edit the ``.c4``,
    next import re-compiles. No edit, the import skips parsing entirely.

This is the canonical "dogfood" path: anything in the stdlib is itself
written in cobra4. If a feature isn't expressive enough to write a
stdlib module without falling back to Python, that's a hint to fix the
language.
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

_STDLIB_DIR = Path(__file__).parent
_PREFIX = "cobra4.stdlib."

# Cache file format:
#   8 bytes magic (b"C4PYC\x00\x01\x02")
#   8 bytes source mtime (uint64, ns)
#   8 bytes source size  (uint64)
#   marshalled code object follows
_MAGIC = b"C4PYC\x00\x01\x02"
_HEADER_FMT = f"<{len(_MAGIC)}sQQ"


def _cache_path(c4_path: Path) -> Path:
    cache_dir = c4_path.parent / "__pycache__"
    cache_dir.mkdir(exist_ok=True)
    return cache_dir / f"{c4_path.stem}.cobra4.pyc"


def _load_cached(c4_path: Path):
    """Return a code object if the cache is valid for ``c4_path``."""
    cache = _cache_path(c4_path)
    if not cache.exists():
        return None
    try:
        st = c4_path.stat()
        with open(cache, "rb") as fh:
            header = fh.read(struct.calcsize(_HEADER_FMT))
            if len(header) != struct.calcsize(_HEADER_FMT):
                return None
            magic, mtime, size = struct.unpack(_HEADER_FMT, header)
            if magic != _MAGIC:
                return None
            if mtime != st.st_mtime_ns or size != st.st_size:
                return None
            return marshal.loads(fh.read())
    except (OSError, ValueError, EOFError):
        return None


def _store_cached(c4_path: Path, code) -> None:
    cache = _cache_path(c4_path)
    st = c4_path.stat()
    try:
        with open(cache, "wb") as fh:
            fh.write(struct.pack(_HEADER_FMT, _MAGIC, st.st_mtime_ns, st.st_size))
            fh.write(marshal.dumps(code))
    except OSError:
        # Cache write is best-effort; failure is not fatal.
        pass


class _C4Loader(importlib.abc.Loader):
    def __init__(self, fullname: str, path: Path) -> None:
        self.fullname = fullname
        self.path = path

    def create_module(self, spec):
        return None  # default module construction

    def exec_module(self, module: types.ModuleType) -> None:
        # Lazy imports to avoid circular import with parser at package load.
        compiled = _load_cached(self.path)
        if compiled is None:
            from cobra4.parser import parse
            from cobra4.lowering import lower
            from cobra4.codegen import generate

            src = self.path.read_text(encoding="utf-8")
            tree = parse(src, source_path=str(self.path))
            code = generate(lower(tree), cobra4_path=str(self.path)).code
            compiled = compile(code, str(self.path), "exec")
            _store_cached(self.path, compiled)
        module.__file__ = str(self.path)
        module.__loader__ = self
        exec(compiled, module.__dict__)


class _C4Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path, target=None):
        if not fullname.startswith(_PREFIX):
            return None
        rel = fullname[len(_PREFIX):]
        c4_path = _STDLIB_DIR / (rel + ".c4")
        if not c4_path.exists():
            return None
        loader = _C4Loader(fullname, c4_path)
        return importlib.machinery.ModuleSpec(fullname, loader, origin=str(c4_path))


# Install the finder once.
if not any(isinstance(f, _C4Finder) for f in sys.meta_path):
    sys.meta_path.insert(0, _C4Finder())


# Convenience: list available stdlib modules.
def list_modules() -> list[str]:
    return sorted(p.stem for p in _STDLIB_DIR.glob("*.c4"))


def clear_cache() -> int:
    """Remove all stdlib .cobra4.pyc cache files. Returns count removed."""
    n = 0
    for c in _STDLIB_DIR.glob("__pycache__/*.cobra4.pyc"):
        try:
            c.unlink()
            n += 1
        except OSError:
            pass
    return n
