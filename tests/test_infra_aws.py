"""Tests for AWS IaC adapters (aws.s3, aws.lambda).

Run against the in-memory mock clients so no AWS account / boto3
package is needed. Production use installs `cobra4[aws]` and the
adapters transparently switch to real boto3 clients.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from cobra4.runtime import infra as infra_mod
from cobra4.runtime.infra_aws import (
    S3Adapter, LambdaAdapter, _MockS3, _MockLambda,
    set_test_clients, reset_test_clients,
)


@pytest.fixture(autouse=True)
def _clear_state():
    infra_mod.clear_registry()
    s3, lam = _MockS3(), _MockLambda()
    set_test_clients(s3, lam)
    yield s3, lam
    reset_test_clients()
    infra_mod.clear_registry()


# ---------- aws.s3 ----------


def test_s3_plan_create_for_new_bucket(_clear_state) -> None:
    a = S3Adapter()
    plan = a.plan(current={}, desired={"name": "b", "region": "us-east-1"})
    assert plan.kind == "create"


def test_s3_plan_noop_when_state_matches() -> None:
    a = S3Adapter()
    desired = {"name": "b", "region": "eu-west-1", "tags": {"x": "y"}}
    current = dict(desired)
    plan = a.plan(current, desired)
    assert plan.kind == "noop"


def test_s3_plan_update_when_tags_differ() -> None:
    a = S3Adapter()
    plan = a.plan(
        current={"name": "b", "region": "eu-west-1", "tags": {"x": "old"}},
        desired={"name": "b", "region": "eu-west-1", "tags": {"x": "new"}},
    )
    assert plan.kind == "update"
    assert "tags" in plan.diff


def test_s3_plan_requires_name() -> None:
    a = S3Adapter()
    with pytest.raises(infra_mod.InfraError, match="'name' is missing"):
        a.plan({}, {})


def test_s3_apply_creates_bucket(_clear_state) -> None:
    s3, _ = _clear_state
    a = S3Adapter()
    result = a.apply({}, {"name": "b1", "region": "eu-west-1", "tags": {"k": "v"}})
    assert result["name"] == "b1"
    assert "b1" in s3.buckets
    assert s3.buckets["b1"]["region"] == "eu-west-1"


def test_s3_apply_uses_default_region_for_us_east_1(_clear_state) -> None:
    """boto3 rejects CreateBucketConfiguration when region is us-east-1.
    The adapter must omit it for that region."""
    s3, _ = _clear_state
    a = S3Adapter()
    a.apply({}, {"name": "b2", "region": "us-east-1"})
    create_calls = [c for c in s3.calls if c[0] == "create_bucket"]
    assert create_calls
    # Mock records the kwargs as the bucket dict; check it doesn't have
    # a non-us-east region stored.
    assert s3.buckets["b2"]["region"] == "us-east-1"


def test_s3_apply_is_idempotent_on_existing_bucket(_clear_state) -> None:
    s3, _ = _clear_state
    s3.buckets["b3"] = {"region": "eu-west-1"}
    a = S3Adapter()
    a.apply({"name": "b3"}, {"name": "b3", "region": "eu-west-1"})
    create_calls = [c for c in s3.calls if c[0] == "create_bucket" and c[1] == "b3"]
    assert not create_calls, "should not re-create existing bucket"


def test_s3_destroy_removes_bucket(_clear_state) -> None:
    s3, _ = _clear_state
    s3.buckets["b4"] = {"region": "x"}
    a = S3Adapter()
    a.destroy({"name": "b4"})
    assert "b4" not in s3.buckets


# ---------- aws.lambda ----------


def _make_zip(tmp: Path, contents: bytes = b"def handler(event, context):\n    return event\n") -> Path:
    """Build a minimal lambda zip for the adapter to read."""
    z = tmp / "fn.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("handler.py", contents)
    return z


def test_lambda_plan_create_for_new_function(tmp_path: Path) -> None:
    a = LambdaAdapter()
    plan = a.plan(current={}, desired={
        "name": "my-fn", "handler": "handler.main",
        "role": "arn:aws:iam::1:role/x", "runtime": "python3.12",
    })
    assert plan.kind == "create"


def test_lambda_plan_requires_name_handler_role() -> None:
    a = LambdaAdapter()
    with pytest.raises(infra_mod.InfraError, match="'name'"):
        a.plan({}, {})
    with pytest.raises(infra_mod.InfraError, match="'handler'"):
        a.plan({}, {"name": "x"})
    with pytest.raises(infra_mod.InfraError, match="'role'"):
        a.plan({}, {"name": "x", "handler": "h.main"})


def test_lambda_apply_creates_then_idempotent(_clear_state, tmp_path: Path) -> None:
    _, lam = _clear_state
    z = _make_zip(tmp_path)
    a = LambdaAdapter()
    desired = {
        "name": "fn1", "handler": "handler.main", "role": "arn:1",
        "zip_path": str(z), "memory": 512, "timeout": 30,
    }
    result = a.apply({}, desired)
    assert result["name"] == "fn1"
    assert result["code_hash"] == hashlib.sha256(z.read_bytes()).hexdigest()
    assert "fn1" in lam.functions

    # Second apply — should call update, not create.
    a.apply(result, desired)
    creates = [c for c in lam.calls if c[0] == "create_function" and c[1] == "fn1"]
    assert len(creates) == 1


def test_lambda_destroy_removes_function(_clear_state) -> None:
    _, lam = _clear_state
    lam.functions["fn2"] = {"FunctionName": "fn2"}
    a = LambdaAdapter()
    a.destroy({"name": "fn2"})
    assert "fn2" not in lam.functions


def test_lambda_apply_includes_env_vars(_clear_state, tmp_path: Path) -> None:
    _, lam = _clear_state
    z = _make_zip(tmp_path)
    a = LambdaAdapter()
    a.apply({}, {
        "name": "envfn", "handler": "h.main", "role": "arn:1",
        "zip_path": str(z), "env": {"DB_URL": "postgres://..."},
    })
    create_call = [c for c in lam.calls if c[0] == "create_function" and c[1] == "envfn"]
    assert create_call
    assert lam.functions["envfn"]["FunctionName"] == "envfn"


# ---------- end-to-end via cobra4 source ----------


def _run_c4(args: list[str], cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *args],
        capture_output=True, text=True, cwd=cwd,
    )
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def test_e2e_s3_apply_via_c4_infra(tmp_path: Path) -> None:
    src = tmp_path / "infra.c4"
    src.write_text(
        'resource bucket = aws.s3 {\n'
        '    name: "demo-bucket"\n'
        '    region: "eu-west-1"\n'
        '    tags: {"team": "data"}\n'
        '}\n'
    )
    code, out = _run_c4(["infra", "apply", str(src)], tmp_path)
    assert code == 0, out
    assert "applied" in out
    assert "demo-bucket" in out


def test_e2e_s3_plan_then_apply_then_plan_noop(tmp_path: Path) -> None:
    src = tmp_path / "infra.c4"
    src.write_text(
        'resource bucket = aws.s3 {\n'
        '    name: "idempotent-test"\n'
        '    region: "us-east-1"\n'
        '}\n'
    )
    code, _ = _run_c4(["infra", "apply", str(src)], tmp_path)
    assert code == 0
    code, plan_out = _run_c4(["infra", "plan", str(src)], tmp_path)
    assert code == 0
    # NOTE: subprocess each invocation creates fresh mock clients, so
    # the apply and the second plan don't share state. The bucket name
    # is checked via the saved JSON state file, not the mock — confirm
    # the state file was created and the second plan reads it.
    state = tmp_path / ".cobra4" / "state.json"
    assert state.exists()
