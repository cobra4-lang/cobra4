"""Secrets backends for cobra4.

API: ``secret("path/to/key")`` returns the secret value as a string.

Backend selection (in order):

1. ``COBRA4_SECRETS_BACKEND`` env var → one of ``env`` / ``file`` / ``vault`` / ``aws-sm`` / ``gcp-sm``.
2. ``[secrets] backend = "..."`` in ``cobra4.toml`` next to the project.
3. Default: ``env`` (read from ``COBRA4_SECRET_<UPPER_PATH>`` env vars).

Backends are intentionally pluggable: ``register_backend("name", fn)``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Callable, Optional


_Backend = Callable[[str], str]
_backends: dict[str, _Backend] = {}
_active_backend: Optional[str] = None


class SecretNotFound(KeyError):
    pass


def register_backend(name: str, fn: _Backend) -> None:
    _backends[name] = fn


def use_backend(name: str) -> None:
    global _active_backend
    if name not in _backends:
        raise ValueError(f"unknown secrets backend '{name}' — registered: {list(_backends)}")
    _active_backend = name


def _resolve_active() -> str:
    if _active_backend is not None:
        return _active_backend
    env = os.environ.get("COBRA4_SECRETS_BACKEND")
    if env:
        return env
    # cobra4.toml [secrets] backend
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        cfg = d / "cobra4.toml"
        if cfg.exists():
            with open(cfg, "rb") as f:
                data = tomllib.load(f)
            backend = data.get("secrets", {}).get("backend")
            if backend:
                return backend
            break
    return "env"


def secret(path: str) -> str:
    from cobra4.runtime.effects import check as _check_effect
    _check_effect("secret")
    backend = _resolve_active()
    if backend not in _backends:
        raise ValueError(f"secrets backend '{backend}' is not registered")
    return _backends[backend](path)


# ---------- env backend ----------


def _env_backend(path: str) -> str:
    env_name = "COBRA4_SECRET_" + path.upper().replace("/", "_").replace("-", "_").replace(".", "_")
    val = os.environ.get(env_name)
    if val is None:
        # Fallback: try the path itself as an env var name.
        val = os.environ.get(path.upper().replace("/", "_"))
    if val is None:
        raise SecretNotFound(f"secret '{path}' not found in env (looked at {env_name})")
    return val


# ---------- file backend ----------


def _file_backend(path: str) -> str:
    """Read from ``~/.cobra4/secrets/<path>`` or a TOML file."""
    base = Path(os.environ.get("COBRA4_SECRETS_DIR") or (Path.home() / ".cobra4" / "secrets"))
    direct = base / path
    if direct.exists():
        return direct.read_text(encoding="utf-8").rstrip("\n")
    # Try TOML file at base/secrets.toml with nested keys.
    toml_path = base / "secrets.toml"
    if toml_path.exists():
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        cur: object = data
        for part in path.split("/"):
            if not isinstance(cur, dict) or part not in cur:
                raise SecretNotFound(f"secret '{path}' not in {toml_path}")
            cur = cur[part]
        if isinstance(cur, str):
            return cur
        raise SecretNotFound(f"secret '{path}' is not a string in {toml_path}")
    raise SecretNotFound(f"no secrets at {direct} or {toml_path}")


# ---------- vault / aws-sm / gcp-sm (lazy stubs) ----------


def _vault_backend(path: str) -> str:
    try:
        import hvac  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "Vault backend requires `pip install hvac`."
        ) from e
    addr = os.environ.get("VAULT_ADDR")
    token = os.environ.get("VAULT_TOKEN")
    if not addr or not token:
        raise RuntimeError("Vault backend requires VAULT_ADDR and VAULT_TOKEN env vars")
    client = hvac.Client(url=addr, token=token)
    mount, _, key = path.partition("/")
    secret_path = key
    resp = client.secrets.kv.v2.read_secret_version(path=secret_path, mount_point=mount or "secret")
    return resp["data"]["data"][secret_path.rsplit("/", 1)[-1]]


def _aws_sm_backend(path: str) -> str:
    try:
        import boto3  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("AWS Secrets Manager backend requires `cobra4[aws]`.") from e
    client = boto3.client("secretsmanager")
    resp = client.get_secret_value(SecretId=path)
    return resp.get("SecretString", "")


def _gcp_sm_backend(path: str) -> str:
    try:
        from google.cloud import secretmanager  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("GCP Secret Manager backend requires `google-cloud-secret-manager`.") from e
    client = secretmanager.SecretManagerServiceClient()
    resp = client.access_secret_version(name=f"{path}/versions/latest")
    return resp.payload.data.decode("utf-8")


# ---------- bootstrapping ----------


register_backend("env", _env_backend)
register_backend("file", _file_backend)
register_backend("vault", _vault_backend)
register_backend("aws-sm", _aws_sm_backend)
register_backend("gcp-sm", _gcp_sm_backend)


# ---------- testing helpers ----------


def reset_for_tests() -> None:
    """Reset to default `env` backend without env overrides."""
    global _active_backend
    _active_backend = None
