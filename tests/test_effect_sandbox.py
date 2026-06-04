"""Tests for the runtime effect sandbox.

Static effect declarations (`with [...]`) catch most issues at check
time. The runtime sandbox catches the rest — when call chains cross
untrusted code or dynamic dispatch.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from cobra4.runtime.effects import (
    EffectViolation,
    current_allowed,
    check,
    with_effects,
)
from cobra4.runtime import log, save, secret, queue

# ---------- runtime API ----------


def test_no_sandbox_means_no_check() -> None:
    """Outside any sandbox, every effect is allowed."""
    assert current_allowed() is None
    check("http")
    check("anything-here")  # passes silently


def test_sandbox_blocks_disallowed_effect() -> None:
    with with_effects("log"):
        check("log")  # OK
        with pytest.raises(EffectViolation, match="'fs'"):
            check("fs")


def test_sandbox_intersects_when_nested() -> None:
    """Nesting cannot elevate — child mask is intersected with parent's."""
    with with_effects("log", "fs"):
        with with_effects("log", "http"):
            assert current_allowed() == frozenset({"log"})
            check("log")
            with pytest.raises(EffectViolation):
                check("http")  # outer didn't allow http
            with pytest.raises(EffectViolation):
                check("fs")  # inner didn't list fs


def test_sandbox_pops_on_exit() -> None:
    assert current_allowed() is None
    with with_effects("log"):
        assert current_allowed() == frozenset({"log"})
    assert current_allowed() is None


def test_sandbox_pops_on_exception() -> None:
    try:
        with with_effects("log"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert current_allowed() is None


def test_sandbox_is_thread_local() -> None:
    """Each thread has its own stack — `each ... in parallel` won't
    bleed effects between workers."""
    seen: dict[str, frozenset | None] = {}
    barrier = threading.Barrier(2)

    def worker(name: str, eff: str) -> None:
        with with_effects(eff):
            barrier.wait()
            seen[name] = current_allowed()

    t1 = threading.Thread(target=worker, args=("a", "log"))
    t2 = threading.Thread(target=worker, args=("b", "fs"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert seen["a"] == frozenset({"log"})
    assert seen["b"] == frozenset({"fs"})


# ---------- builtin enforcement ----------


def test_log_is_blocked_outside_allowed_set() -> None:
    with with_effects("fs"):  # log NOT included
        with pytest.raises(EffectViolation, match="'log'"):
            log("nope")


def test_save_is_blocked_outside_fs(tmp_path: Path) -> None:
    target = tmp_path / "x.json"
    with with_effects("log"):
        with pytest.raises(EffectViolation, match="'fs'"):
            save({"k": 1}, str(target))


def test_queue_is_blocked_outside_time() -> None:
    with with_effects("log"):
        with pytest.raises(EffectViolation, match="'time'"):
            queue("test_blocked")


def test_secret_is_blocked_outside_secret() -> None:
    """`secret()` requires the `secret` effect."""
    with with_effects("log"):
        with pytest.raises(EffectViolation, match="'secret'"):
            secret("does-not-exist")


def test_log_works_inside_log_sandbox() -> None:
    with with_effects("log"):
        log("hi")  # no exception


# ---------- end-to-end via cobra4 source ----------


def _run_c4(tmp_path: Path, src: str) -> tuple[int, str, str]:
    f = tmp_path / "prog.c4"
    f.write_text(src)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", "run", str(f)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_e2e_sandbox_block_compiles_and_runs(tmp_path: Path) -> None:
    src = "sandbox [log] {\n" '    log("inside")\n' "}\n"
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "inside" in stderr


def test_e2e_sandbox_violation_can_be_caught(tmp_path: Path) -> None:
    src = (
        "sandbox [log] {\n"
        "    try {\n"
        '        save({"k": 1}, "./x.json")\n'
        "    } catch EffectViolation as e {\n"
        '        log("blocked", err=str(e))\n'
        "    }\n"
        "}\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "blocked" in stderr
    # The save MUST NOT have run
    assert not (tmp_path / "x.json").exists()


def test_e2e_outside_sandbox_unrestricted(tmp_path: Path) -> None:
    """Programs that don't use `sandbox` see no behavior change — every
    existing example must still work, no false positives."""
    src = (
        'rows = [{"a": 1}, {"a": 2}]\n'
        'save(rows, "./out.json")\n'
        'log("done", n=len(rows))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert (tmp_path / "out.json").exists()
