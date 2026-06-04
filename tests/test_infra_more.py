"""Tests for the additional IaC adapters: aws.rds, aws.iam, k8s.deployment."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cobra4.runtime import infra as infra_mod
from cobra4.runtime.infra_aws import (
    RDSAdapter,
    IAMRoleAdapter,
    _MockRDS,
    _MockIAM,
    _MockS3,
    _MockLambda,
    set_test_clients,
    reset_test_clients,
)
from cobra4.runtime.infra_k8s import (
    K8sDeploymentAdapter,
    _build_manifest,
    set_test_runner,
    reset_test_runner,
)

# ---------- shared fixtures ----------


@pytest.fixture(autouse=True)
def _isolation():
    infra_mod.clear_registry()
    set_test_clients(_MockS3(), _MockLambda())  # avoid network for AWS
    yield
    reset_test_clients()
    reset_test_runner()
    infra_mod.clear_registry()


# ---------- aws.rds ----------


def test_rds_plan_create_for_new_instance() -> None:
    a = RDSAdapter()
    plan = a.plan(
        {},
        {
            "name": "db1",
            "engine": "postgres",
            "instance_class": "db.t3.medium",
            "allocated_storage": 10,
            "master_username": "u",
            "master_password": "p",
        },
    )
    assert plan.kind == "create"


def test_rds_plan_requires_critical_fields() -> None:
    a = RDSAdapter()
    with pytest.raises(infra_mod.InfraError, match="'engine'"):
        a.plan({}, {"name": "x"})


def test_rds_apply_then_noop() -> None:
    rds = _MockRDS()
    iam = _MockIAM()
    from cobra4.runtime import infra_aws

    infra_aws._TEST_CLIENTS = (_MockS3(), _MockLambda(), rds, iam)

    a = RDSAdapter()
    desired = {
        "name": "db1",
        "engine": "postgres",
        "instance_class": "db.t3.medium",
        "allocated_storage": 10,
        "master_username": "u",
        "master_password": "p",
        "publicly_accessible": False,
    }
    result = a.apply({}, desired)
    assert result["name"] == "db1"
    assert "db1" in rds.instances
    # Re-plan should be NOOP
    next_plan = a.plan(result, desired)
    assert next_plan.kind == "noop"


def test_rds_destroy_calls_skip_final_snapshot() -> None:
    rds = _MockRDS()
    iam = _MockIAM()
    from cobra4.runtime import infra_aws

    infra_aws._TEST_CLIENTS = (_MockS3(), _MockLambda(), rds, iam)

    rds.instances["db1"] = {"DBInstanceIdentifier": "db1"}
    a = RDSAdapter()
    a.destroy({"name": "db1"})
    assert "db1" not in rds.instances


# ---------- aws.iam ----------


def test_iam_plan_create_for_new_role() -> None:
    a = IAMRoleAdapter()
    plan = a.plan(
        {},
        {
            "name": "role-x",
            "assume_role_policy": {"Version": "2012-10-17", "Statement": []},
        },
    )
    assert plan.kind == "create"


def test_iam_apply_attaches_policies() -> None:
    rds = _MockRDS()
    iam = _MockIAM()
    from cobra4.runtime import infra_aws

    infra_aws._TEST_CLIENTS = (_MockS3(), _MockLambda(), rds, iam)

    a = IAMRoleAdapter()
    a.apply(
        {},
        {
            "name": "role-y",
            "assume_role_policy": {"Version": "2012-10-17"},
            "policies": [
                "arn:aws:iam::aws:policy/ReadOnlyAccess",
                "arn:aws:iam::aws:policy/AWSLambda_FullAccess",
            ],
        },
    )
    assert "role-y" in iam.roles
    assert iam.attached["role-y"] == {
        "arn:aws:iam::aws:policy/ReadOnlyAccess",
        "arn:aws:iam::aws:policy/AWSLambda_FullAccess",
    }


def test_iam_apply_detaches_removed_policies() -> None:
    rds = _MockRDS()
    iam = _MockIAM()
    from cobra4.runtime import infra_aws

    infra_aws._TEST_CLIENTS = (_MockS3(), _MockLambda(), rds, iam)

    a = IAMRoleAdapter()
    a.apply({}, {"name": "r", "assume_role_policy": {}, "policies": ["a", "b"]})
    # Second apply with only "b" should detach "a"
    a.apply({}, {"name": "r", "assume_role_policy": {}, "policies": ["b"]})
    assert iam.attached["r"] == {"b"}


def test_iam_destroy_detaches_before_delete() -> None:
    rds = _MockRDS()
    iam = _MockIAM()
    from cobra4.runtime import infra_aws

    infra_aws._TEST_CLIENTS = (_MockS3(), _MockLambda(), rds, iam)

    iam.roles["r"] = {"RoleName": "r"}
    iam.attached["r"] = {"arn:x"}
    a = IAMRoleAdapter()
    a.destroy({"name": "r"})
    assert "r" not in iam.roles
    assert "r" not in iam.attached


# ---------- k8s.deployment ----------


def test_k8s_build_manifest_includes_replicas_and_image() -> None:
    src = _build_manifest({"name": "x", "image": "img:v1", "replicas": 3})
    m = json.loads(src)
    assert m["kind"] == "Deployment"
    assert m["metadata"]["name"] == "x"
    assert m["spec"]["replicas"] == 3
    assert m["spec"]["template"]["spec"]["containers"][0]["image"] == "img:v1"


def test_k8s_build_manifest_includes_ports_and_env() -> None:
    src = _build_manifest(
        {
            "name": "x",
            "image": "i",
            "ports": [8080, 9090],
            "env": {"K": "V", "K2": "V2"},
        }
    )
    m = json.loads(src)
    container = m["spec"]["template"]["spec"]["containers"][0]
    assert [p["containerPort"] for p in container["ports"]] == [8080, 9090]
    env_kv = {e["name"]: e["value"] for e in container["env"]}
    assert env_kv == {"K": "V", "K2": "V2"}


def test_k8s_plan_requires_name_and_image() -> None:
    a = K8sDeploymentAdapter()
    with pytest.raises(infra_mod.InfraError, match="'name'"):
        a.plan({}, {})
    with pytest.raises(infra_mod.InfraError, match="'image'"):
        a.plan({}, {"name": "x"})


def test_k8s_plan_noop_when_manifest_unchanged() -> None:
    desired = {"name": "x", "image": "i", "replicas": 1}
    manifest = _build_manifest(desired)
    import hashlib

    h = hashlib.sha256(manifest.encode()).hexdigest()
    current = {"name": "x", "manifest_hash": h}
    a = K8sDeploymentAdapter()
    plan = a.plan(current, desired)
    assert plan.kind == "noop"


class _StubKubectl:
    def __init__(self) -> None:
        self.applies: list[str] = []
        self.deletes: list[tuple[str, str | None]] = []

    def apply(self, manifest_yaml, *, namespace):
        self.applies.append(manifest_yaml)

    def delete(self, name, *, namespace):
        self.deletes.append((name, namespace))


def test_k8s_apply_calls_kubectl_with_manifest() -> None:
    stub = _StubKubectl()
    set_test_runner(stub)
    a = K8sDeploymentAdapter()
    result = a.apply({}, {"name": "fe", "image": "img:v1", "replicas": 2})
    assert len(stub.applies) == 1
    assert "Deployment" in stub.applies[0]
    assert "manifest_hash" in result
    assert result["replicas"] == 2


def test_k8s_destroy_calls_delete() -> None:
    stub = _StubKubectl()
    set_test_runner(stub)
    a = K8sDeploymentAdapter()
    a.destroy({"name": "fe", "namespace": "prod"})
    assert stub.deletes == [("fe", "prod")]


# ---------- end-to-end via cobra4 source ----------


def _run_c4(
    args: list[str], cwd: Path, env_extra: dict | None = None
) -> tuple[int, str]:
    import os

    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-m", "cobra4.cli", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    return proc.returncode, proc.stdout + "\n" + proc.stderr


def test_e2e_rds_plan(tmp_path: Path) -> None:
    """End-to-end plan for an RDS resource through `c4 infra plan`."""
    src = tmp_path / "infra.c4"
    src.write_text(
        "resource db = aws.rds {\n"
        '    name: "test-pg"\n'
        '    engine: "postgres"\n'
        '    instance_class: "db.t3.micro"\n'
        "    allocated_storage: 20\n"
        '    master_username: "u"\n'
        '    master_password: "p"\n'
        "}\n"
    )
    code, out = _run_c4(["infra", "plan", str(src)], tmp_path)
    assert code == 0, out
    assert "CREATE" in out
    assert "test-pg" in out


def test_e2e_iam_plan(tmp_path: Path) -> None:
    src = tmp_path / "infra.c4"
    src.write_text(
        "resource role = aws.iam {\n"
        '    name: "test-role"\n'
        '    assume_role_policy: {"Version": "2012-10-17"}\n'
        '    policies: ["arn:aws:iam::aws:policy/ReadOnlyAccess"]\n'
        "}\n"
    )
    code, out = _run_c4(["infra", "plan", str(src)], tmp_path)
    assert code == 0, out
    assert "CREATE" in out and "test-role" in out
