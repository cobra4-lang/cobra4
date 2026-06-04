"""Fleet operations: inventories, hosts, remote command execution.

cobra4 ships with a tiny inventory format and a ``run`` smart-fn that
dispatches to local subprocess by default and to SSH when given a Host.

Inventory file (TOML), default location ``./cobra4.toml``:

    [hosts.web1]
    addr = "web1.example.com"
    user = "deploy"

    [hosts.web2]
    addr = "10.0.0.5"
    user = "deploy"

    [groups]
    prod = ["web1", "web2"]

Pattern matching for ``inventory("prod-*")``:

- Exact group name → its host list.
- Glob pattern (``*``) over host names → matching hosts.
- ``"all"`` → all hosts.
"""

from __future__ import annotations

import fnmatch
import os
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from cobra4.runtime.smart import SmartFn, make_smart

# ---------- Host ----------


@dataclass
class Host:
    name: str
    addr: str = "localhost"
    user: Optional[str] = None
    port: int = 22
    extra: dict = field(default_factory=dict)

    @property
    def is_local(self) -> bool:
        return self.addr in ("localhost", "127.0.0.1", "::1")

    def __str__(self) -> str:
        if self.user:
            return f"{self.user}@{self.addr}:{self.port}"
        return f"{self.addr}:{self.port}"


# ---------- inventory loading ----------


_loaded_inventory: dict[str, Any] | None = None
_inventory_path: Path | None = None


def _find_inventory_file() -> Path | None:
    """Locate the inventory file by walking up from cwd."""
    here = Path.cwd().resolve()
    for d in [here, *here.parents]:
        candidate = d / "cobra4.toml"
        if candidate.exists():
            return candidate
    home = Path.home() / ".cobra4" / "inventory.toml"
    if home.exists():
        return home
    return None


def _load_inventory() -> dict[str, Any]:
    global _loaded_inventory, _inventory_path
    if _loaded_inventory is not None:
        return _loaded_inventory
    path = _find_inventory_file()
    if path is None:
        _loaded_inventory = {"hosts": {}, "groups": {}}
        return _loaded_inventory
    with open(path, "rb") as f:
        data = tomllib.load(f)
    _loaded_inventory = {
        "hosts": data.get("hosts", {}),
        "groups": data.get("groups", {}),
    }
    _inventory_path = path
    return _loaded_inventory


def _build_host(name: str, spec: dict) -> Host:
    return Host(
        name=name,
        addr=spec.get("addr", name),
        user=spec.get("user"),
        port=spec.get("port", 22),
        extra={k: v for k, v in spec.items() if k not in ("addr", "user", "port")},
    )


def reset_inventory_cache() -> None:
    """Test helper — re-read inventory on next call."""
    global _loaded_inventory, _inventory_path
    _loaded_inventory = None
    _inventory_path = None


def inventory(pattern: str = "all") -> list[Host]:
    """Resolve a pattern to a list of :class:`Host`.

    - ``"all"`` → every host.
    - exact group name (e.g. ``"prod"``) → its hosts.
    - glob over host names (e.g. ``"prod-*"``, ``"web?"``) → matching.
    """
    inv = _load_inventory()
    hosts: dict[str, Host] = {
        n: _build_host(n, spec) for n, spec in inv["hosts"].items()
    }

    if pattern == "all":
        return list(hosts.values())
    if pattern in inv["groups"]:
        return [hosts[name] for name in inv["groups"][pattern] if name in hosts]
    matched = [h for n, h in hosts.items() if fnmatch.fnmatchcase(n, pattern)]
    return matched


def add_host(host: Host) -> None:
    """Programmatically add a host to the loaded inventory (for tests/scripts)."""
    inv = _load_inventory()
    inv["hosts"][host.name] = {
        "addr": host.addr,
        "user": host.user,
        "port": host.port,
        **host.extra,
    }


# ---------- run (smart) ----------


@dataclass
class CommandResult:
    cmd: str
    host: Host
    stdout: str
    stderr: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_local(
    cmd,
    *,
    host: Optional[Host] = None,
    timeout: float = 60.0,
    shell: bool = False,
) -> CommandResult:
    """Execute a command locally.

    Default semantics (``shell=False``):
        - ``cmd`` is a list → exec'd directly with no shell.
        - ``cmd`` is a string → split with ``shlex`` and exec'd directly.
        Safe against injection from untrusted ``cmd`` content.

    Shell semantics (``shell=True``):
        - ``cmd`` (string) is interpreted by ``/bin/sh`` (or ``cmd.exe`` on
          Windows). Pipes, redirects, glob expansion, env-var substitution
          all work — but the caller is responsible for quoting.

    Choose ``shell=True`` deliberately when you need shell features.
    Don't pass user-supplied content through it.
    """
    h = host or Host(name="local", addr="localhost")
    cmd_str = (
        cmd if isinstance(cmd, str) else " ".join(shlex.quote(str(c)) for c in cmd)
    )
    if shell:
        argv = cmd_str
    else:
        argv = (
            cmd
            if isinstance(cmd, list)
            else shlex.split(cmd_str, posix=(os.name != "nt"))
        )
    proc = subprocess.run(
        argv,
        shell=shell,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        cmd=cmd_str,
        host=h,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


def _run_ssh(cmd: str, *, host: Host, timeout: float = 60.0) -> CommandResult:
    """SSH execution. Prefers ``paramiko`` if installed (more capable —
    rich auth, sudo, sftp); falls back to system ``ssh``.

    Install paramiko via ``pip install cobra4[ssh]``.
    """
    try:
        import paramiko  # type: ignore

        return _run_paramiko(cmd, host=host, timeout=timeout, paramiko=paramiko)
    except ImportError:
        pass
    target = f"{host.user}@{host.addr}" if host.user else host.addr
    full = ["ssh", "-p", str(host.port), "-o", "BatchMode=yes", target, cmd]
    proc = subprocess.run(
        full,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return CommandResult(
        cmd=cmd,
        host=host,
        stdout=proc.stdout,
        stderr=proc.stderr,
        returncode=proc.returncode,
    )


def _run_paramiko(cmd: str, *, host: Host, timeout: float, paramiko) -> CommandResult:
    """SSH via paramiko: ssh-agent / id_rsa / id_ed25519 picked up automatically.

    Host-key policy:

    - Default: **strict** — reject unknown host keys, require an entry in
      ``~/.ssh/known_hosts`` (loaded via ``load_system_host_keys``).
    - Override per-host with ``host.extra["host_key_policy"] = "auto"``
      to fall back to ``AutoAddPolicy`` (useful for ephemeral CI hosts;
      makes you MITM-vulnerable if used carelessly).
    - Or globally with ``COBRA4_SSH_HOST_KEY_POLICY=auto``.
    """
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    policy_name = (
        host.extra.get("host_key_policy")
        or os.environ.get("COBRA4_SSH_HOST_KEY_POLICY")
        or "strict"
    ).lower()
    if policy_name == "auto":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    elif policy_name in ("warn", "warning"):
        client.set_missing_host_key_policy(paramiko.WarningPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=host.addr,
            port=host.port,
            username=host.user,
            timeout=timeout,
            allow_agent=True,
            look_for_keys=True,
        )
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        return CommandResult(
            cmd=cmd,
            host=host,
            stdout=stdout.read().decode("utf-8", errors="replace"),
            stderr=stderr.read().decode("utf-8", errors="replace"),
            returncode=exit_code,
        )
    finally:
        client.close()


def copy_to_host(
    local_path: str, remote_path: str, *, host: Host, timeout: float = 60.0
) -> bool:
    """Copy a local file to ``remote_path`` on ``host`` via SFTP (paramiko).

    Returns ``True`` on success. Falls back to ``scp`` if paramiko is missing.
    """
    try:
        import paramiko  # type: ignore
    except ImportError:
        target = f"{host.user}@{host.addr}" if host.user else host.addr
        full = ["scp", "-P", str(host.port), local_path, f"{target}:{remote_path}"]
        return subprocess.run(full, timeout=timeout).returncode == 0
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    policy_name = (
        host.extra.get("host_key_policy")
        or os.environ.get("COBRA4_SSH_HOST_KEY_POLICY")
        or "strict"
    ).lower()
    if policy_name == "auto":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect(
            hostname=host.addr, port=host.port, username=host.user, timeout=timeout
        )
        with client.open_sftp() as sftp:
            sftp.put(local_path, remote_path)
        return True
    finally:
        client.close()


def _run_default(
    cmd: Any, *, host: Optional[Host] = None, **kwargs: Any
) -> CommandResult:
    """Default ``run``: takes a string command, runs locally."""
    if isinstance(cmd, list):
        cmd = " ".join(shlex.quote(str(c)) for c in cmd)
    if host is None or host.is_local:
        return _run_local(str(cmd), host=host, **kwargs)
    return _run_ssh(str(cmd), host=host, **kwargs)


run: SmartFn = make_smart("run", default=_run_default)
run.required_effect = "ssh"


# Convenience: run on each host in parallel.
def fan_out(cmd: str, hosts: list[Host], *, workers: int = 16) -> list[CommandResult]:
    from cobra4.runtime.concurrency import parallel_for

    return parallel_for(hosts, lambda h: run(cmd, host=h), workers=workers)
