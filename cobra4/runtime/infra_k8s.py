"""Kubernetes adapter for cobra4 IaC.

The ``k8s.deployment`` adapter generates a Deployment manifest from the
resource fields and applies it via ``kubectl``. State is the manifest
hash + the metadata `name` / `namespace`, so re-applying with the same
fields is a NOOP.

This is intentionally minimal: one resource kind, no rollout-status
polling, no helm. For Helm-style packages, use a higher-level adapter
that shells out to `helm upgrade --install`.

Test mode: when ``set_test_runner(...)`` is called, all ``kubectl``
invocations route through a stub that records calls and returns
synthetic responses. Production uses the real ``kubectl`` binary on
PATH.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from typing import Any, Optional

from cobra4.runtime.infra import Action, InfraError, register_adapter

# ---------- kubectl invocation ----------


_TEST_RUNNER: Optional[Any] = None


def set_test_runner(runner: Any) -> None:
    """Inject a stub for kubectl. Used by the test suite to record calls
    instead of executing the real binary."""
    global _TEST_RUNNER
    _TEST_RUNNER = runner


def reset_test_runner() -> None:
    global _TEST_RUNNER
    _TEST_RUNNER = None


class _RealKubectl:
    """Thin wrapper around the real ``kubectl`` CLI."""

    def apply(self, manifest_yaml: str, *, namespace: Optional[str]) -> None:
        if shutil.which("kubectl") is None:
            raise InfraError(
                "k8s.deployment: `kubectl` not found on PATH. "
                "Install kubectl or use set_test_runner() in tests."
            )
        args = ["kubectl", "apply", "-f", "-"]
        if namespace:
            args += ["-n", namespace]
        result = subprocess.run(
            args,
            input=manifest_yaml,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise InfraError(f"kubectl apply failed: {result.stderr.strip()}")

    def delete(self, name: str, *, namespace: Optional[str]) -> None:
        if shutil.which("kubectl") is None:
            return
        args = ["kubectl", "delete", "deployment", name, "--ignore-not-found"]
        if namespace:
            args += ["-n", namespace]
        subprocess.run(args, capture_output=True)


def _runner() -> Any:
    return _TEST_RUNNER if _TEST_RUNNER is not None else _RealKubectl()


# ---------- manifest builder ----------


def _build_manifest(desired: dict) -> str:
    """Render the resource fields as a Deployment YAML string. We build
    JSON (which is valid YAML) so we don't need a yaml library."""
    name = desired["name"]
    replicas = int(desired.get("replicas", 1))
    image = desired["image"]
    namespace = desired.get("namespace")

    container = {
        "name": name,
        "image": image,
    }
    if "ports" in desired:
        container["ports"] = [{"containerPort": int(p)} for p in desired["ports"]]
    if "env" in desired:
        container["env"] = [
            {"name": k, "value": str(v)} for k, v in desired["env"].items()
        ]
    if "resources" in desired:
        container["resources"] = desired["resources"]

    manifest: dict[str, Any] = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {"containers": [container]},
            },
        },
    }
    if namespace:
        manifest["metadata"]["namespace"] = namespace
    return json.dumps(manifest, indent=2, sort_keys=True)


# ---------- adapter ----------


class K8sDeploymentAdapter:
    """``k8s.deployment``: required ``name`` and ``image``. Optional
    ``replicas`` (default 1), ``namespace``, ``ports`` (list[int]),
    ``env`` (dict[str,str]), ``resources`` (raw k8s resources block)."""

    def plan(self, current: dict, desired: dict) -> Action:
        name = desired.get("name")
        if not name:
            raise InfraError("k8s.deployment: required field 'name' is missing")
        if not desired.get("image"):
            raise InfraError(f"k8s.deployment {name!r}: 'image' is required")

        manifest = _build_manifest(desired)
        new_hash = hashlib.sha256(manifest.encode()).hexdigest()

        if not current:
            return Action(kind="create", notes=f"create deployment {name}")
        if current.get("manifest_hash") == new_hash:
            return Action(kind="noop", notes=f"deployment {name} matches state")
        return Action(
            kind="update",
            diff={"manifest_hash": (current.get("manifest_hash"), new_hash)},
            notes=f"update deployment {name}",
        )

    def apply(self, current: dict, desired: dict) -> dict:
        name = desired["name"]
        namespace = desired.get("namespace")
        manifest = _build_manifest(desired)
        _runner().apply(manifest, namespace=namespace)
        return {
            "name": name,
            "namespace": namespace,
            "image": desired["image"],
            "replicas": int(desired.get("replicas", 1)),
            "manifest_hash": hashlib.sha256(manifest.encode()).hexdigest(),
        }

    def destroy(self, current: dict) -> None:
        name = current.get("name")
        if not name:
            return
        _runner().delete(name, namespace=current.get("namespace"))


register_adapter("k8s.deployment", K8sDeploymentAdapter())


__all__ = [
    "K8sDeploymentAdapter",
    "set_test_runner",
    "reset_test_runner",
]
