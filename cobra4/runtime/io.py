"""IO runtime: ``read`` and ``save`` as smart-dispatch functions.

Format and source are inferred from URI scheme + extension. Handlers are
registered at import time; libraries (or user cobra4 code) can register
more by calling ``read.register(...)`` / ``save.register(...)``.

M1 supported sources:

- ``./`` / ``file://`` — local filesystem
- ``https://`` / ``http://`` — HTTP GET / PUT (PUT only as a courtesy stub)

M1 supported formats: csv, json, jsonl, txt, md.
Optional: parquet (requires ``cobra4[data]``).
Optional: s3 (requires ``cobra4[aws]``).
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from cobra4.runtime.smart import SmartFn, make_smart


def _atomic_write_bytes(target: str, data: bytes) -> str:
    """Write ``data`` to ``target`` atomically.

    Writes to a temp file in the same directory (so ``os.replace`` stays
    on the same filesystem), fsyncs, then renames. The rename is atomic
    on POSIX and on NTFS via ``MoveFileExW``. On crash the user is left
    with either the old file or the new one — never a half-written file.
    """
    dir_ = os.path.dirname(os.path.abspath(target)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".c4tmp_", dir=dir_)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:  # fsync isn't available on every fs
                pass
        os.replace(tmp, target)
        return target
    except BaseException:
        # Best-effort: clean up the temp file if we crashed before rename.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(target: str, text: str) -> str:
    return _atomic_write_bytes(target, text.encode("utf-8"))


# ---------- read ----------


def _read_default(target: Any, **_) -> Any:
    raise ValueError(
        f"read: don't know how to read {type(target).__name__} ({target!r}). "
        f"Register a handler with read.register(scheme=..., ext=..., fn=...)."
    )


read: SmartFn = make_smart("read", default=_read_default)


def _open_local_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _open_local_bytes(path: str) -> bytes:
    return Path(path).read_bytes()


def _read_local_csv(target: str, **_) -> list[dict]:
    text = _open_local_text(target)
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def _read_local_jsonl(target: str, **_) -> list[Any]:
    text = _open_local_text(target)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _read_local_json(target: str, **_) -> Any:
    return json.loads(_open_local_text(target))


def _read_local_text(target: str, **_) -> str:
    return _open_local_text(target)


def _read_http(target: str, **kwargs: Any) -> Any:
    import requests  # local import — keeps top-level fast

    resp = requests.get(target, timeout=kwargs.get("timeout", 30))
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if "json" in ctype:
        return resp.json()
    if ctype == "text/csv" or target.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(resp.text))
        return list(reader)
    return resp.text


def _read_s3(target: str, **kwargs: Any) -> Any:
    import boto3  # type: ignore

    bucket, key = _parse_s3_uri(target)
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    ext = (key.rsplit(".", 1)[-1] if "." in key else "").lower()
    if ext == "csv":
        return list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    if ext == "jsonl":
        return [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]
    if ext == "json":
        return json.loads(body.decode("utf-8"))
    if ext == "parquet":
        try:
            import pyarrow.parquet as pq  # type: ignore
            return pq.read_table(io.BytesIO(body)).to_pylist()
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("parquet support requires `cobra4[data]`") from e
    return body.decode("utf-8")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    return bucket, key


def _read_local_parquet(target: str, **_) -> list[dict]:
    try:
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("parquet support requires `cobra4[data]`") from e
    return pq.read_table(target).to_pylist()


# ---------- save ----------


def _save_default(value: Any, target: Any, **_) -> Any:
    raise ValueError(
        f"save: don't know how to save to {type(target).__name__} ({target!r}). "
        f"Register a handler with save.register(scheme=..., ext=..., fn=...)."
    )


save: SmartFn = make_smart("save", default=_save_default)


def _save_local_csv(value: Any, target: str, **_) -> str:
    if not value:
        return _atomic_write_text(target, "")
    rows = list(value)
    if isinstance(rows[0], dict):
        keys = list(rows[0].keys())
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerows(rows)
    return _atomic_write_text(target, buf.getvalue())


def _save_local_json(value: Any, target: str, **_) -> str:
    return _atomic_write_text(target, json.dumps(value, indent=2))


def _save_local_jsonl(value: Any, target: str, **_) -> str:
    return _atomic_write_text(target, "\n".join(json.dumps(x) for x in value))


def _save_local_text(value: Any, target: str, **_) -> str:
    return _atomic_write_text(target, str(value))


def _save_local_parquet(value: Any, target: str, **_) -> str:
    try:
        import pyarrow as pa  # type: ignore
        import pyarrow.parquet as pq  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("parquet support requires `cobra4[data]`") from e
    rows = list(value)
    if rows and isinstance(rows[0], dict):
        cols: dict[str, list[Any]] = {k: [] for k in rows[0].keys()}
        for r in rows:
            for k in cols:
                cols[k].append(r.get(k))
        table = pa.table(cols)
    else:
        table = pa.table({"value": list(rows)})
    pq.write_table(table, target)
    return target


def _save_s3(value: Any, target: str, **_) -> str:
    import boto3  # type: ignore

    bucket, key = _parse_s3_uri(target)
    ext = (key.rsplit(".", 1)[-1] if "." in key else "").lower()
    if ext == "csv":
        rows = list(value)
        if rows and isinstance(rows[0], dict):
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            data = buf.getvalue().encode("utf-8")
        else:
            buf = io.StringIO()
            csv.writer(buf).writerows(rows)
            data = buf.getvalue().encode("utf-8")
    elif ext == "jsonl":
        data = "\n".join(json.dumps(x) for x in value).encode("utf-8")
    elif ext == "json":
        data = json.dumps(value, indent=2).encode("utf-8")
    elif ext == "parquet":
        try:
            import pyarrow as pa  # type: ignore
            import pyarrow.parquet as pq  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("parquet support requires `cobra4[data]`") from e
        rows = list(value)
        if rows and isinstance(rows[0], dict):
            cols: dict[str, list[Any]] = {k: [] for k in rows[0].keys()}
            for r in rows:
                for k in cols:
                    cols[k].append(r.get(k))
            table = pa.table(cols)
        else:
            table = pa.table({"value": list(rows)})
        sink = io.BytesIO()
        pq.write_table(table, sink)
        data = sink.getvalue()
    else:
        data = str(value).encode("utf-8")
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data)
    return target


# ---------- handler registration (boot) ----------


def _register_handlers() -> None:
    # read — local
    read.register(_read_local_csv, type=str, scheme="file", ext="csv", name="local-csv")
    read.register(_read_local_json, type=str, scheme="file", ext="json", name="local-json")
    read.register(_read_local_jsonl, type=str, scheme="file", ext="jsonl", name="local-jsonl")
    read.register(_read_local_text, type=str, scheme="file", ext="txt", name="local-txt")
    read.register(_read_local_text, type=str, scheme="file", ext="md", name="local-md")
    read.register(_read_local_parquet, type=str, scheme="file", ext="parquet", name="local-parquet")
    # Generic fallback for any local file: read as UTF-8 text.
    read.register(_read_local_text, type=str, scheme="file", name="local-text-fallback")
    # read — http
    read.register(_read_http, type=str, scheme="http", name="http")
    read.register(_read_http, type=str, scheme="https", name="https")
    # read — s3
    read.register(_read_s3, type=str, scheme="s3", name="s3")
    # read — Path objects
    read.register(lambda p, **kw: read(str(p), **kw), type=Path, name="path-redirect", priority=1)

    # save — local (note: save's first arg is value, second is target — but
    # SmartFn dispatches on first arg. We invert: save dispatches on TARGET
    # via a custom `when` predicate that inspects args[1].)
    save.register(_save_local_csv, when=_target_is("file", "csv"), name="local-csv-save")
    save.register(_save_local_json, when=_target_is("file", "json"), name="local-json-save")
    save.register(_save_local_jsonl, when=_target_is("file", "jsonl"), name="local-jsonl-save")
    save.register(_save_local_text, when=_target_is("file", "txt"), name="local-txt-save")
    save.register(_save_local_text, when=_target_is("file", "md"), name="local-md-save")
    save.register(_save_local_parquet, when=_target_is("file", "parquet"), name="local-parquet-save")
    save.register(_save_s3, when=_target_is_scheme("s3"), name="s3-save")


def _target_is(scheme: str, ext: str):
    """Custom predicate over **the second** positional arg (the target URI)."""

    def pred(_value: Any) -> bool:
        # We can only see the first arg via SmartFn's predicate API. The
        # save dispatcher hooks in via _save_call_shim below.
        return True  # see _save_call_shim — actual filtering happens there

    pred.scheme = scheme  # type: ignore[attr-defined]
    pred.ext = ext  # type: ignore[attr-defined]
    return pred


def _target_is_scheme(scheme: str):
    def pred(_value: Any) -> bool:
        return True

    pred.scheme = scheme  # type: ignore[attr-defined]
    pred.ext = None  # type: ignore[attr-defined]
    return pred


# `save` dispatches on the *target* URI, not the value. We override its
# __call__ behavior by replacing the resolution path: route through a
# helper SmartFn keyed on the second arg.
_save_inner = make_smart("_save_target")


def _save_dispatch(value: Any, target: Any, **kwargs: Any) -> Any:
    # Forward to inner smart-fn keyed on `target`.
    return _save_inner(target, value, **kwargs)


def _install_save_dispatch() -> None:
    # Migrate the registered save-handlers onto _save_inner, keyed on target.
    moved: list = []
    for h in save.handlers:
        scheme = getattr(h.pred.custom, "scheme", None) if h.pred.custom else None
        ext = getattr(h.pred.custom, "ext", None) if h.pred.custom else None
        # adapter: target-first → value-first
        original = h.fn

        def make_adapter(orig=original):
            def _adapter(target, value, **kw):
                return orig(value, target, **kw)
            return _adapter

        _save_inner.register(make_adapter(), type=str, scheme=scheme, ext=ext, name=h.name)
        moved.append(h)
    # Replace `save`'s default with our dispatcher.
    save._handlers.clear()
    save._cache.clear()
    save.default = _save_dispatch


_register_handlers()
_install_save_dispatch()
