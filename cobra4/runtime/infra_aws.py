"""AWS adapters for cobra4 declarative infrastructure.

Two adapters, registered automatically when this module is imported:

- ``aws.s3``     — manage S3 buckets (create / tag / delete).
- ``aws.lambda`` — manage Lambda functions (create / update / delete).

Both adapters delegate to ``boto3`` when ``cobra4[aws]`` is installed.
For tests / offline runs they fall back to a dict-backed mock client
that records calls — enough to drive plan/apply/destroy round-trips
without an AWS account.

This is MVP coverage: the surface that 90% of users hit. Bucket
policies, CORS, lifecycle rules, encryption, environment-variable
manifests, VPC config — all valid future work, not in 0.4.

Example cobra4 source:

.. code-block:: cobra4

    resource artifacts = aws.s3 {
        name: "my-app-artifacts"
        region: "eu-west-1"
        tags: {"team": "data"}
    }

    resource api = aws.lambda {
        name: "my-app-api"
        handler: "handler.main"
        runtime: "python3.12"
        role: "arn:aws:iam::123:role/my-lambda"
        memory: 512
        timeout: 30
        zip_path: "./build/api.zip"
    }
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Optional

from cobra4.runtime.infra import Action, register_adapter, InfraError


# ---------- client factory (real boto3 or mock) ----------


def _make_clients() -> tuple[Any, Any]:
    """Return ``(s3_client, lambda_client)``. Uses boto3 when available;
    otherwise the mock client below.

    Override at test-time via :func:`set_test_clients` so adapters can
    be exercised without AWS creds and without monkeypatching boto3."""
    if _TEST_CLIENTS is not None:
        return _TEST_CLIENTS
    try:
        import boto3  # type: ignore
    except ImportError:
        return _MockS3(), _MockLambda()
    return boto3.client("s3"), boto3.client("lambda")


_TEST_CLIENTS: Optional[tuple[Any, Any]] = None


def set_test_clients(s3: Any, lam: Any) -> None:
    """Inject mock clients (used by the test suite)."""
    global _TEST_CLIENTS
    _TEST_CLIENTS = (s3, lam)


def reset_test_clients() -> None:
    global _TEST_CLIENTS
    _TEST_CLIENTS = None


# ---------- mock clients (offline default) ----------


class _MockS3:
    """In-memory stand-in for boto3 S3 client. Implements only the
    minimum the S3Adapter calls."""

    def __init__(self) -> None:
        self.buckets: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def head_bucket(self, *, Bucket: str) -> dict:
        self.calls.append(("head_bucket", Bucket))
        if Bucket not in self.buckets:
            from botocore.exceptions import ClientError  # type: ignore
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")
        return {}

    def create_bucket(self, *, Bucket: str, CreateBucketConfiguration=None, **_) -> dict:
        self.calls.append(("create_bucket", Bucket))
        self.buckets[Bucket] = {"region": (CreateBucketConfiguration or {}).get("LocationConstraint", "us-east-1")}
        return {"Location": f"http://{Bucket}.mock"}

    def put_bucket_tagging(self, *, Bucket: str, Tagging: dict) -> dict:
        self.calls.append(("put_bucket_tagging", Bucket, Tagging))
        if Bucket in self.buckets:
            self.buckets[Bucket]["tags"] = Tagging
        return {}

    def delete_bucket(self, *, Bucket: str) -> dict:
        self.calls.append(("delete_bucket", Bucket))
        self.buckets.pop(Bucket, None)
        return {}


class _MockLambda:
    def __init__(self) -> None:
        self.functions: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def get_function(self, *, FunctionName: str) -> dict:
        self.calls.append(("get_function", FunctionName))
        if FunctionName not in self.functions:
            from botocore.exceptions import ClientError  # type: ignore
            raise ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "GetFunction")
        return {"Configuration": dict(self.functions[FunctionName])}

    def create_function(self, **kw) -> dict:
        self.calls.append(("create_function", kw["FunctionName"]))
        self.functions[kw["FunctionName"]] = {
            "FunctionName": kw["FunctionName"],
            "Handler": kw.get("Handler"),
            "Role": kw.get("Role"),
            "Runtime": kw.get("Runtime"),
            "MemorySize": kw.get("MemorySize"),
            "Timeout": kw.get("Timeout"),
        }
        return {"FunctionArn": f"arn:aws:lambda:mock::function:{kw['FunctionName']}"}

    def update_function_code(self, *, FunctionName: str, ZipFile: bytes = b"", **_) -> dict:
        self.calls.append(("update_function_code", FunctionName))
        return {}

    def update_function_configuration(self, **kw) -> dict:
        self.calls.append(("update_function_configuration", kw["FunctionName"]))
        if kw["FunctionName"] in self.functions:
            self.functions[kw["FunctionName"]].update({k: v for k, v in kw.items() if k != "FunctionName"})
        return {}

    def delete_function(self, *, FunctionName: str) -> dict:
        self.calls.append(("delete_function", FunctionName))
        self.functions.pop(FunctionName, None)
        return {}


# ---------- aws.s3 adapter ----------


class S3Adapter:
    """Manage an S3 bucket. Required field: ``name``. Optional: ``region``, ``tags``."""

    def plan(self, current: dict, desired: dict) -> Action:
        name = desired.get("name")
        if not name:
            raise InfraError("aws.s3: required field 'name' is missing")

        if not current:
            return Action(kind="create", notes=f"create s3://{name}")

        diff: dict[str, tuple[Any, Any]] = {}
        for f in ("region", "tags"):
            if current.get(f) != desired.get(f):
                diff[f] = (current.get(f), desired.get(f))
        if not diff:
            return Action(kind="noop", notes=f"s3://{name} matches state")
        return Action(kind="update", diff=diff, notes=f"update s3://{name}")

    def apply(self, current: dict, desired: dict) -> dict:
        s3, _ = _make_clients()
        name = desired["name"]
        region = desired.get("region")
        tags = desired.get("tags", {})

        # Create if needed
        try:
            s3.head_bucket(Bucket=name)
            exists = True
        except Exception:
            exists = False

        if not exists:
            kwargs: dict = {"Bucket": name}
            if region and region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
            s3.create_bucket(**kwargs)

        # Tags (always reapply for idempotency)
        if tags:
            s3.put_bucket_tagging(
                Bucket=name,
                Tagging={"TagSet": [{"Key": k, "Value": str(v)} for k, v in tags.items()]},
            )

        return {"name": name, "region": region, "tags": tags}

    def destroy(self, current: dict) -> None:
        s3, _ = _make_clients()
        name = current.get("name")
        if name:
            try:
                s3.delete_bucket(Bucket=name)
            except Exception:  # pragma: no cover
                pass


# ---------- aws.lambda adapter ----------


class LambdaAdapter:
    """Manage an AWS Lambda function. Required: ``name``, ``handler``,
    ``role``, ``zip_path``. Optional: ``runtime``, ``memory``,
    ``timeout``, ``env``."""

    def plan(self, current: dict, desired: dict) -> Action:
        name = desired.get("name")
        if not name:
            raise InfraError("aws.lambda: required field 'name' is missing")
        if not desired.get("handler"):
            raise InfraError(f"aws.lambda {name!r}: 'handler' is required")
        if not desired.get("role"):
            raise InfraError(f"aws.lambda {name!r}: 'role' is required")

        if not current:
            return Action(kind="create", notes=f"create lambda {name}")

        diff: dict[str, tuple[Any, Any]] = {}
        for f in ("handler", "role", "runtime", "memory", "timeout", "code_hash"):
            if current.get(f) != desired.get(f) and not (
                f == "code_hash" and desired.get(f) is None
            ):
                diff[f] = (current.get(f), desired.get(f))
        if not diff:
            return Action(kind="noop", notes=f"lambda {name} matches state")
        return Action(kind="update", diff=diff, notes=f"update lambda {name}")

    def apply(self, current: dict, desired: dict) -> dict:
        _, lam = _make_clients()
        name = desired["name"]
        zip_path = desired.get("zip_path")
        zip_bytes = b""
        code_hash = None
        if zip_path:
            zb = Path(zip_path).read_bytes()
            zip_bytes = zb
            code_hash = hashlib.sha256(zb).hexdigest()

        try:
            lam.get_function(FunctionName=name)
            exists = True
        except Exception:
            exists = False

        params = {
            "FunctionName": name,
            "Runtime": desired.get("runtime", "python3.12"),
            "Role": desired["role"],
            "Handler": desired["handler"],
            "MemorySize": desired.get("memory", 512),
            "Timeout": desired.get("timeout", 30),
        }
        if desired.get("env"):
            params["Environment"] = {"Variables": dict(desired["env"])}

        if exists:
            lam.update_function_configuration(**params)
            if zip_bytes:
                lam.update_function_code(FunctionName=name, ZipFile=zip_bytes)
        else:
            lam.create_function(**params, Code={"ZipFile": zip_bytes})

        return {
            "name": name,
            "handler": params["Handler"],
            "role": params["Role"],
            "runtime": params["Runtime"],
            "memory": params["MemorySize"],
            "timeout": params["Timeout"],
            "code_hash": code_hash,
            "env": desired.get("env", {}),
        }

    def destroy(self, current: dict) -> None:
        _, lam = _make_clients()
        name = current.get("name")
        if name:
            try:
                lam.delete_function(FunctionName=name)
            except Exception:  # pragma: no cover
                pass


register_adapter("aws.s3", S3Adapter())
register_adapter("aws.lambda", LambdaAdapter())


__all__ = [
    "S3Adapter", "LambdaAdapter",
    "set_test_clients", "reset_test_clients",
    "_MockS3", "_MockLambda",
]
