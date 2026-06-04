"""Tests for the new stdlib modules shipped in 0.5.0.

Each test exercises the cobra4 source via a small `c4 run` invocation —
the modules are written in cobra4, not Python, so unit-testing them
via Python imports wouldn't validate the cobra4 round-trip.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


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


# ---------- module import ----------


@pytest.mark.parametrize(
    "mod",
    [
        "crypto",
        "random",
        "math",
        "path",
        "url",
        "uuid",
        "cache",
        "retry",
        "validate",
    ],
)
def test_module_imports_cleanly(tmp_path: Path, mod: str) -> None:
    """Each new stdlib module must parse, codegen, and import without
    error — the same `use` line the user would write."""
    src = f'use cobra4.stdlib.{mod} as m\nlog("ok", mod="{mod}")\n'
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, f"{mod} failed to import: {stderr}"


# ---------- crypto ----------


def test_crypto_sha256_deterministic(tmp_path: Path) -> None:
    src = "use cobra4.stdlib.crypto as c\n" 'log("h", v=c.sha256("hello"))\n'
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    # Known SHA-256 of "hello"
    assert "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824" in stderr


def test_crypto_hmac_verify_constant_time(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.crypto as c\n"
        'k = "key"\n'
        'm = "message"\n'
        "d = c.hmac_sha256(k, m)\n"
        'log("v", ok=c.hmac_verify(k, m, d), bad=c.hmac_verify(k, m, "deadbeef"))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "ok=True" in stderr
    assert "bad=False" in stderr


def test_crypto_b64_roundtrips(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.crypto as c\n"
        'enc = c.b64_encode("hello world")\n'
        "dec = c.b64_decode(enc)\n"
        'log("r", dec=dec.decode("utf-8"))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert 'dec="hello world"' in stderr


def test_crypto_token_hex_length(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.crypto as c\n" "t = c.token_hex(16)\n" 'log("r", n=len(t))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "n=32" in stderr  # 16 bytes → 32 hex chars


# ---------- math ----------


def test_math_percentiles(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.math as m\n"
        "xs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]\n"
        'log("r", p50=m.p50(xs), p90=m.p90(xs), p95=m.p95(xs))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "p50=5.5" in stderr
    # p90 of 1..10 with linear interp = 9.1
    assert "p90=9.1" in stderr


def test_math_clamp(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.math as m\n"
        'log("r", a=m.clamp(5, 0, 10), b=m.clamp(-3, 0, 10), c=m.clamp(99, 0, 10))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "a=5" in stderr and "b=0" in stderr and "c=10" in stderr


def test_math_approx_eq(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.math as m\n"
        'log("r", a=m.approx_eq(0.1 + 0.2, 0.3), b=m.approx_eq(1.0, 2.0))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "a=True" in stderr and "b=False" in stderr


# ---------- path ----------


def test_path_join_split_roundtrip(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.path as p\n"
        'log("r", j=p.join("a", "b", "c"), st=p.stem("/x/y/file.c4"))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "j=a/b/c" in stderr
    assert "st=file" in stderr


# ---------- url ----------


def test_url_parse_extracts_components(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.url as u\n"
        'p = u.parse("https://example.com:8080/api/v1?k=1&k=2#section")\n'
        'log("r", host=p["host"], port=p["port"], path=p["path"])\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "host=example.com" in stderr
    assert "port=8080" in stderr
    assert "path=/api/v1" in stderr


def test_url_build_compose(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.url as u\n"
        'result = u.build("https", "x.io", path="/q", query={"k": "v"})\n'
        'log("r", url=result)\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "https://x.io/q?k=v" in stderr


# ---------- uuid ----------


def test_uuid_new_is_36_chars(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.uuid as uu\n"
        "u = uu.new()\n"
        'log("r", n=len(u), valid=uu.is_valid(u))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "n=36" in stderr
    assert "valid=True" in stderr


def test_uuid_v5_deterministic(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.uuid as uu\n"
        'a = uu.v5("dns", "example.com")\n'
        'b = uu.v5("dns", "example.com")\n'
        'log("r", eq=(a == b))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "eq=True" in stderr


# ---------- random ----------


def test_random_choice_returns_from_sequence(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.random as r\n"
        'x = r.choice(["a", "b", "c"])\n'
        'log("r", in_set=(x in ["a", "b", "c"]))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "in_set=True" in stderr


def test_random_sample_no_replacement(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.random as r\n"
        "s = r.sample([1, 2, 3, 4, 5], 5)\n"
        'log("r", n=len(s), unique=(len(set(s)) == 5))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "n=5" in stderr and "unique=True" in stderr


# ---------- validate ----------


def test_validate_passes_for_valid_input(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.validate as v\n"
        'ok = v.validate("hello", v.is_str, v.min_length(3))\n'
        'log("r", ok=ok)\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "ok=hello" in stderr


def test_validate_raises_with_aggregated_errors(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.validate as v\n"
        "try {\n"
        '    v.validate("hi", v.is_int, v.min_length(10))\n'
        "} catch v.ValidationError as e {\n"
        '    log("caught", n=len(e.errors))\n'
        "}\n"
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    # Two errors: not int, too short
    assert "n=2" in stderr


def test_validate_schema(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.validate as v\n"
        "sch = v.schema({\n"
        '    "name": [v.is_str, v.min_length(1)],\n'
        '    "age":  [v.is_int, v.in_range(0, 150)],\n'
        "})\n"
        'result = sch({"name": "ada", "age": 36})\n'
        'log("r", ok=(result["name"] == "ada"))\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "ok=True" in stderr


# ---------- cache ----------


def test_cache_lru_memoizes(tmp_path: Path) -> None:
    """An @lru-decorated function should compute once per unique args."""
    src = (
        "use cobra4.stdlib.cache as ca\n"
        'calls = {"n": 0}\n'
        "@ca.lru(maxsize=10)\n"
        "fn slow(x) {\n"
        '    calls["n"] += 1\n'
        "    return x * 2\n"
        "}\n"
        "slow(5)\n"
        "slow(5)\n"
        "slow(5)\n"
        'log("r", calls=calls["n"])\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "calls=1" in stderr


# ---------- retry ----------


def test_retry_eventually_succeeds(tmp_path: Path) -> None:
    src = (
        "use cobra4.stdlib.retry as r\n"
        'state = {"n": 0}\n'
        "fn flaky() {\n"
        '    state["n"] += 1\n'
        '    if state["n"] < 3 { raise ValueError("not yet") }\n'
        '    return "ok"\n'
        "}\n"
        "result = r.with_retry(flaky, max_attempts=5, backoff=0.001, jitter=False)\n"
        'log("r", result=result, attempts=state["n"])\n'
    )
    code, _, stderr = _run_c4(tmp_path, src)
    assert code == 0, stderr
    assert "result=ok" in stderr
    assert "attempts=3" in stderr
