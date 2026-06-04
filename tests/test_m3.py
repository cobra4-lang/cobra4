"""M3 tests: fleet, secrets, deploy."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

import importlib

from cobra4.runtime import fleet, secrets

deploy = importlib.import_module("cobra4.runtime.deploy")  # the module
from cobra4.runtime.deploy import DeployTarget, aws, gcp

# ---------- Fleet ----------


def test_fleet_inventory_loads_from_toml():
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "cobra4.toml"
        cfg.write_text(
            '[hosts.web1]\naddr = "10.0.0.1"\nuser = "deploy"\n\n'
            '[hosts.web2]\naddr = "10.0.0.2"\n\n'
            '[groups]\nprod = ["web1", "web2"]\n',
            encoding="utf-8",
        )
        old_cwd = os.getcwd()
        os.chdir(d)
        try:
            fleet.reset_inventory_cache()
            hosts = fleet.inventory("prod")
            assert {h.name for h in hosts} == {"web1", "web2"}
            assert hosts[0].user == "deploy" or hosts[1].user == "deploy"
            # glob pattern
            web = fleet.inventory("web*")
            assert len(web) == 2
            # all
            all_hosts = fleet.inventory("all")
            assert len(all_hosts) == 2
        finally:
            os.chdir(old_cwd)
            fleet.reset_inventory_cache()


def test_fleet_run_local():
    h = fleet.Host(name="local", addr="localhost")
    if os.name == "nt":
        result = fleet.run("echo cobra4-test", host=h)
    else:
        result = fleet.run("echo cobra4-test", host=h)
    assert result.ok
    assert "cobra4-test" in result.stdout


# ---------- Secrets ----------


def test_secrets_env_backend():
    secrets.reset_for_tests()
    os.environ["COBRA4_SECRETS_BACKEND"] = "env"
    os.environ["COBRA4_SECRET_DB_PASSWORD"] = "supersecret"
    try:
        assert secrets.secret("db/password") == "supersecret"
    finally:
        del os.environ["COBRA4_SECRETS_BACKEND"]
        del os.environ["COBRA4_SECRET_DB_PASSWORD"]


def test_secrets_env_missing_raises():
    secrets.reset_for_tests()
    os.environ["COBRA4_SECRETS_BACKEND"] = "env"
    try:
        with pytest.raises(secrets.SecretNotFound):
            secrets.secret("nope/missing")
    finally:
        del os.environ["COBRA4_SECRETS_BACKEND"]


def test_secrets_file_backend():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "db"))
        Path(d, "db", "password").write_text("from-file\n", encoding="utf-8")
        secrets.reset_for_tests()
        os.environ["COBRA4_SECRETS_BACKEND"] = "file"
        os.environ["COBRA4_SECRETS_DIR"] = d
        try:
            assert secrets.secret("db/password") == "from-file"
        finally:
            del os.environ["COBRA4_SECRETS_BACKEND"]
            del os.environ["COBRA4_SECRETS_DIR"]


def test_secrets_custom_backend():
    secrets.register_backend("test", lambda p: f"test:{p}")
    secrets.use_backend("test")
    try:
        assert secrets.secret("anything") == "test:anything"
    finally:
        secrets.reset_for_tests()


# ---------- Deploy ----------


def test_deploy_dry_run_default():
    """Default mode logs the plan and does not call the adapter."""
    os.environ.pop("COBRA4_DEPLOY_DRY_RUN", None)
    handler = lambda req: {"ok": True}
    target = (
        aws.lambda_(region="us-east-1")
        if hasattr(aws, "lambda_")
        else aws.__getattr__("lambda")(region="us-east-1")
    )
    entry = deploy.deploy(handler, target)
    assert entry is not None


def test_deploy_target_builder():
    t = aws.__getattr__("lambda")(region="eu-west-1", name="api")
    assert isinstance(t, DeployTarget)
    assert t.name == "aws.lambda"
    assert t.args["region"] == "eu-west-1"


def test_env_from_reads_dotenv():
    with tempfile.TemporaryDirectory() as d:
        env = Path(d, ".env")
        env.write_text("FOO=bar\nBAZ=qux\n# comment\n\n", encoding="utf-8")
        result = deploy.env_from(str(env))
        assert result == {"FOO": "bar", "BAZ": "qux"}


def test_register_custom_adapter():
    called = []

    def my_adapter(handler, target, args):
        called.append(("called", handler, target.name, args))

    deploy.register_adapter("test.local", my_adapter)
    target = DeployTarget(name="test.local", args={"x": 1})
    os.environ["COBRA4_DEPLOY_DRY_RUN"] = "0"
    try:
        deploy.deploy(lambda r: None, target)
        assert called and called[0][0] == "called"
    finally:
        os.environ.pop("COBRA4_DEPLOY_DRY_RUN", None)
