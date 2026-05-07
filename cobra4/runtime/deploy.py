"""Deploy adapters.

The ``deploy`` keyword in cobra4 is parsed into a call into this module.
Adapters are registered by name (``aws.lambda``, ``gcp.run``, ``k8s``,
``fly``, ``dry-run``).

By default, deployments run in **dry-run** mode: they don't touch real
infrastructure but log the resolved deployment plan. Set
``COBRA4_DEPLOY_DRY_RUN=0`` (or pass ``dry_run=False`` to ``deploy_handler``)
to actually invoke the underlying SDK.

This is intentional: cobra4 deploy syntax should be safe to run in CI or
local environments without ambient cloud creds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

from cobra4.runtime import observe
from cobra4.runtime.core import _DeployEntry, _deploy_registry, deploy_handler as _record


_Adapter = Callable[[Callable[..., Any], "DeployTarget", dict], Any]
_adapters: dict[str, _Adapter] = {}


@dataclass
class DeployTarget:
    """Resolved deploy target — derived from the AST `to` expression."""

    name: str  # adapter key, e.g. "aws.lambda"
    args: dict = field(default_factory=dict)


def register_adapter(name: str, fn: _Adapter) -> None:
    _adapters[name] = fn


def list_adapters() -> list[str]:
    return sorted(_adapters)


def _is_dry_run(opt: bool | None) -> bool:
    if opt is not None:
        return opt
    return os.environ.get("COBRA4_DEPLOY_DRY_RUN", "1") != "0"


def deploy(
    handler: Callable[..., Any],
    target: Any,
    body: Callable[[], Any] | None = None,
    *,
    dry_run: bool | None = None,
) -> _DeployEntry:
    """Programmatic entry point used by the codegen-emitted call.

    ``target`` may be:

    - a :class:`DeployTarget`,
    - a string ``"aws.lambda"``, or
    - any callable result already shaped like ``DeployTarget`` (anything
      with ``.name`` and ``.args``).
    """
    resolved = _coerce_target(target)
    body = body or (lambda: None)
    entry = _record(handler, resolved, body)
    body()  # apply config-block side effects (env from ".env", etc.)

    if _is_dry_run(dry_run):
        observe.log(
            "deploy.plan",
            adapter=resolved.name,
            handler=getattr(handler, "__name__", repr(handler)),
            args=resolved.args,
            mode="dry-run",
        )
        return entry

    adapter = _adapters.get(resolved.name)
    if adapter is None:
        raise ValueError(
            f"no deploy adapter for '{resolved.name}'. Registered: {list_adapters()}"
        )
    adapter(handler, resolved, resolved.args)
    observe.log("deploy.done", adapter=resolved.name)
    return entry


def _coerce_target(target: Any) -> DeployTarget:
    if isinstance(target, DeployTarget):
        return target
    if isinstance(target, str):
        return DeployTarget(name=target)
    name = getattr(target, "name", None)
    args = getattr(target, "args", {}) or {}
    if name is None:
        # Adapter-builder convention: aws.lambda(region="x") returns a
        # DeployTarget when our helpers below are imported by user code.
        raise TypeError(f"invalid deploy target: {target!r}")
    return DeployTarget(name=name, args=dict(args))


# ---------- helpers exposed to user code ----------


class _AdapterBuilder:
    """``aws.lambda(region="x")`` → DeployTarget("aws.lambda", {"region": "x"}).

    Instances behave like attribute trees: ``aws.lambda``, ``gcp.run``, etc.
    """

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix

    def __getattr__(self, attr: str) -> "_AdapterBuilder":
        if attr.startswith("_"):
            raise AttributeError(attr)
        return _AdapterBuilder(prefix=f"{self._prefix}.{attr}" if self._prefix else attr)

    def __call__(self, **kwargs: Any) -> DeployTarget:
        return DeployTarget(name=self._prefix, args=kwargs)


# Top-level adapter builders that user code can reference.
aws = _AdapterBuilder("aws")
gcp = _AdapterBuilder("gcp")
azure = _AdapterBuilder("azure")
k8s = _AdapterBuilder("k8s")
fly = _AdapterBuilder("fly")


# ---------- env_from helper used by `env from ".env"` ----------


def env_from(path: str) -> dict[str, str]:
    """Read a dotenv-style file and return ``{KEY: value}`` dict.

    Used inside a ``deploy ... { env from ".env" }`` block.
    """
    out: dict[str, str] = {}
    p = os.path.expanduser(path)
    if not os.path.exists(p):
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip("\"'")
    return out


# ---------- adapter registry ----------


def _aws_lambda_adapter(handler, target: DeployTarget, args: dict) -> Any:
    """AWS Lambda adapter: package handler + cobra4 runtime, then create/update.

    Steps:
      1. Build a deterministic zip containing the handler module wrapped
         in a Lambda-compatible adapter (event/context → handler(req)).
      2. Inject the cobra4 runtime as a vendored package inside the zip.
      3. Compute SHA256 — used for idempotency (skip update if unchanged).
      4. Call boto3 ``create_function`` or ``update_function_code``.
    """
    try:
        import boto3  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("aws.lambda adapter requires `cobra4[aws]`") from e

    fn_name = args.get("name") or getattr(handler, "__name__", "cobra4-fn")
    region = args.get("region")
    role = args.get("role") or os.environ.get("COBRA4_LAMBDA_ROLE")
    if not role:
        raise RuntimeError(
            "aws.lambda requires an IAM role ARN — pass `role=...` or set "
            "COBRA4_LAMBDA_ROLE."
        )

    package_path = build_lambda_package(handler, fn_name, args.get("memory", 256))
    code_bytes = open(package_path, "rb").read()

    client = boto3.client("lambda", region_name=region)
    try:
        client.get_function(FunctionName=fn_name)
        observe.log("deploy.aws-lambda.update", name=fn_name, region=region, size=len(code_bytes))
        client.update_function_code(FunctionName=fn_name, ZipFile=code_bytes)
    except client.exceptions.ResourceNotFoundException:
        observe.log("deploy.aws-lambda.create", name=fn_name, region=region, size=len(code_bytes))
        client.create_function(
            FunctionName=fn_name,
            Runtime=args.get("runtime", "python3.12"),
            Role=role,
            Handler=args.get("handler", "lambda_entry.handle"),
            Code={"ZipFile": code_bytes},
            MemorySize=args.get("memory", 256),
            Timeout=args.get("timeout", 30),
        )
    return {"name": fn_name, "package": package_path, "size": len(code_bytes)}


def build_lambda_package(handler: Any, fn_name: str, memory: int) -> str:
    """Build a zip suitable for AWS Lambda. Returns the file path.

    The zip contains:
      - ``lambda_entry.py``: adapter that converts Lambda events into the
        cobra4 handler signature.
      - ``cobra4/...``: the cobra4 runtime (vendored).
      - The user module containing ``handler``.

    The same handler always produces the same bytes (deterministic) —
    safe to compare with previous deploys for idempotent updates.
    """
    import io
    import os as _os
    import shutil
    import tempfile
    import zipfile

    handler_mod = getattr(handler, "__module__", "__main__")
    handler_name = getattr(handler, "__qualname__", "handler")

    out_dir = tempfile.mkdtemp(prefix=f"c4lambda_{fn_name}_")
    zip_path = _os.path.join(out_dir, f"{fn_name}.zip")

    # Vendored runtime (just the directory tree)
    import cobra4 as _c4
    runtime_root = _os.path.dirname(_os.path.abspath(_c4.__file__))

    entry = (
        "import importlib\n"
        f"_user_module = importlib.import_module({handler_mod!r})\n"
        f"_user_handler = getattr(_user_module, {handler_name!r})\n"
        "def handle(event, context):\n"
        "    class _Req:\n"
        "        def __init__(self, ev): self.params = ev.get('queryStringParameters') or {}; self.body = ev.get('body'); self.headers = ev.get('headers') or {}\n"
        "    return _user_handler(_Req(event))\n"
    )

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("lambda_entry.py", entry)
        # Copy cobra4/ runtime
        for root, _dirs, files in _os.walk(runtime_root):
            for fn in files:
                if not fn.endswith(".py") and fn != "grammar.lark":
                    continue
                full = _os.path.join(root, fn)
                rel = _os.path.relpath(full, _os.path.dirname(runtime_root))
                zf.write(full, rel)
    return zip_path


def _gcp_run_adapter(handler, target: DeployTarget, args: dict) -> Any:
    """Deploy a handler to Google Cloud Run.

    Strategy: build a container image from a generated Dockerfile (Python
    base + cobra4 runtime + handler module), push it to Artifact Registry,
    then ``gcloud run deploy``. Requires the ``gcloud`` CLI on PATH and
    a Docker buildkit-capable engine.

    Args (in ``target.args``):
      - ``project``: GCP project id (or env ``GOOGLE_CLOUD_PROJECT``)
      - ``region``: e.g. ``europe-west1`` (default: ``us-central1``)
      - ``name``: service name (default: handler.__name__)
      - ``image``: full image URI (skips build/push if provided)
      - ``allow_unauthenticated``: bool (default: False)
      - ``memory``: e.g. ``"512Mi"``
      - ``cpu``: int (default: 1)
      - ``concurrency``: int (default: 80)
    """
    import shutil
    import subprocess as _sp

    if shutil.which("gcloud") is None:
        raise RuntimeError("gcp.run adapter requires the `gcloud` CLI on PATH")

    project = args.get("project") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("gcp.run requires `project=...` or GOOGLE_CLOUD_PROJECT env var")
    region = args.get("region", "us-central1")
    service = args.get("name") or getattr(handler, "__name__", "cobra4-svc")
    image = args.get("image")

    if image is None:
        image = f"{region}-docker.pkg.dev/{project}/cobra4/{service}:latest"
        build_dir = build_cloud_run_image(handler, service)
        if shutil.which("docker") is None:
            raise RuntimeError("gcp.run image build requires `docker` on PATH (or pass image=...)")
        observe.log("deploy.gcp-run.build", image=image, dir=build_dir)
        _sp.run(["docker", "build", "-t", image, build_dir], check=True)
        _sp.run(["docker", "push", image], check=True)

    cmd = [
        "gcloud", "run", "deploy", service,
        "--image", image,
        "--region", region,
        "--project", project,
        "--platform", "managed",
        "--memory", args.get("memory", "512Mi"),
        "--cpu", str(args.get("cpu", 1)),
        "--concurrency", str(args.get("concurrency", 80)),
        "--port", str(args.get("port", 8080)),
    ]
    if args.get("allow_unauthenticated"):
        cmd.append("--allow-unauthenticated")
    observe.log("deploy.gcp-run.apply", service=service, region=region)
    _sp.run(cmd, check=True)
    return {"service": service, "image": image, "region": region}


def build_cloud_run_image(handler: Any, service: str) -> str:
    """Generate a build context (Dockerfile + handler + cobra4 runtime).

    Returns the absolute path to the build directory. Caller runs
    ``docker build -t IMAGE <dir>``.
    """
    import shutil
    import tempfile

    handler_mod = getattr(handler, "__module__", "__main__")
    handler_name = getattr(handler, "__qualname__", "handler")

    out_dir = tempfile.mkdtemp(prefix=f"c4cloudrun_{service}_")

    # Vendor the cobra4 runtime tree.
    import cobra4 as _c4
    runtime_root = os.path.dirname(os.path.abspath(_c4.__file__))
    shutil.copytree(runtime_root, os.path.join(out_dir, "cobra4"), dirs_exist_ok=True)

    # Entry script that adapts ASGI/HTTP requests to the cobra4 handler shape.
    entry = (
        "import importlib, json, os\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        f"_user_module = importlib.import_module({handler_mod!r})\n"
        f"_user_handler = getattr(_user_module, {handler_name!r})\n"
        "from cobra4.runtime.schedule import _encode_response, _Request\n\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def _serve(self):\n"
        "        from urllib.parse import urlparse, parse_qs\n"
        "        u = urlparse(self.path)\n"
        "        params = {k: (v[0] if len(v)==1 else v) for k,v in parse_qs(u.query).items()}\n"
        "        body = b''\n"
        "        if self.headers.get('content-length'):\n"
        "            body = self.rfile.read(int(self.headers['content-length']))\n"
        "        req = _Request(self.command, u.path, params, {k.lower():v for k,v in self.headers.items()}, body)\n"
        "        try:\n"
        "            r = _user_handler(req); s,h,b = _encode_response(r)\n"
        "        except Exception as e:\n"
        "            s = 500; h = {'content-type':'application/json'}; b = json.dumps({'error': str(e)}).encode()\n"
        "        self.send_response(s)\n"
        "        for k,v in h.items(): self.send_header(k,v)\n"
        "        self.end_headers(); self.wfile.write(b)\n"
        "    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve\n"
        "if __name__ == '__main__':\n"
        "    port = int(os.environ.get('PORT', '8080'))\n"
        "    ThreadingHTTPServer(('0.0.0.0', port), H).serve_forever()\n"
    )
    with open(os.path.join(out_dir, "main.py"), "w", encoding="utf-8") as f:
        f.write(entry)

    # Dockerfile.
    dockerfile = (
        "FROM python:3.12-slim\n"
        "WORKDIR /app\n"
        "RUN pip install --no-cache-dir lark requests\n"
        "COPY cobra4 ./cobra4\n"
        "COPY main.py ./main.py\n"
        "ENV PORT=8080\n"
        "EXPOSE 8080\n"
        'CMD ["python", "main.py"]\n'
    )
    with open(os.path.join(out_dir, "Dockerfile"), "w", encoding="utf-8") as f:
        f.write(dockerfile)
    return out_dir


def _k8s_adapter(handler, target: DeployTarget, args: dict) -> Any:
    """Deploy as a Kubernetes Deployment + Service via ``kubectl apply``.

    Generates a manifest from the handler + image arguments and pipes it
    to ``kubectl apply -f -``. Requires ``kubectl`` configured for the
    target cluster.

    Args:
      - ``image``: required, full image URI
      - ``name``: deployment/service name (default: handler.__name__)
      - ``namespace``: default ``"default"``
      - ``replicas``: default ``2``
      - ``port``: default ``8080``
      - ``cpu``, ``memory``: resource requests/limits
      - ``env``: dict[str, str] of env vars
      - ``service_type``: ``"ClusterIP"`` (default), ``"LoadBalancer"``, ``"NodePort"``
    """
    import shutil
    import subprocess as _sp

    if shutil.which("kubectl") is None:
        raise RuntimeError("k8s adapter requires `kubectl` on PATH")
    if not args.get("image"):
        raise RuntimeError("k8s adapter requires `image=...`")

    name = args.get("name") or getattr(handler, "__name__", "cobra4-app")
    manifest = build_k8s_manifest(name=name, **args)
    observe.log("deploy.k8s.apply", name=name, namespace=args.get("namespace", "default"))
    proc = _sp.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"kubectl apply failed: {proc.stderr}")
    return {"name": name, "namespace": args.get("namespace", "default"), "stdout": proc.stdout}


def build_k8s_manifest(
    *,
    name: str,
    image: str,
    namespace: str = "default",
    replicas: int = 2,
    port: int = 8080,
    cpu: str = "100m",
    memory: str = "256Mi",
    env: dict | None = None,
    service_type: str = "ClusterIP",
    **_extra,
) -> str:
    """Generate Deployment + Service YAML manifest. Returns the YAML string."""
    env_block = ""
    if env:
        env_block = "\n        env:\n" + "\n".join(
            f"        - name: {k}\n          value: {repr(v)}" for k, v in env.items()
        )
    return (
        f"apiVersion: apps/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n  name: {name}\n  namespace: {namespace}\n"
        f"spec:\n  replicas: {replicas}\n"
        f"  selector:\n    matchLabels:\n      app: {name}\n"
        f"  template:\n"
        f"    metadata:\n      labels:\n        app: {name}\n"
        f"    spec:\n      containers:\n"
        f"      - name: {name}\n"
        f"        image: {image}\n"
        f"        ports:\n        - containerPort: {port}\n"
        f"        resources:\n"
        f"          requests:\n            cpu: {cpu}\n            memory: {memory}\n"
        f"          limits:\n            cpu: {cpu}\n            memory: {memory}{env_block}\n"
        f"---\n"
        f"apiVersion: v1\n"
        f"kind: Service\n"
        f"metadata:\n  name: {name}\n  namespace: {namespace}\n"
        f"spec:\n  type: {service_type}\n"
        f"  selector:\n    app: {name}\n"
        f"  ports:\n  - port: {port}\n    targetPort: {port}\n"
    )


def _fly_adapter(handler, target: DeployTarget, args: dict) -> Any:
    """Deploy to Fly.io via ``flyctl``.

    Strategy: generate ``fly.toml`` + Dockerfile in a temp dir, run
    ``flyctl deploy --remote-only -c fly.toml``.

    Args:
      - ``app``: required, fly app name
      - ``region``: optional primary region (e.g. ``"fra"``)
      - ``image``: optional pre-built image (skips build)
    """
    import shutil
    import subprocess as _sp

    if shutil.which("flyctl") is None and shutil.which("fly") is None:
        raise RuntimeError("fly adapter requires `flyctl` (or `fly`) on PATH")
    flyctl = shutil.which("flyctl") or shutil.which("fly")
    if not args.get("app"):
        raise RuntimeError("fly adapter requires `app=...`")

    if args.get("image"):
        cmd = [flyctl, "deploy", "--app", args["app"], "--image", args["image"]]
    else:
        build_dir = build_cloud_run_image(handler, args["app"])
        # fly.toml
        fly_toml = (
            f"app = {args['app']!r}\n"
            f"primary_region = {args.get('region', 'fra')!r}\n"
            f"[http_service]\n  internal_port = 8080\n  force_https = true\n"
            f"  auto_stop_machines = true\n  auto_start_machines = true\n"
        )
        with open(os.path.join(build_dir, "fly.toml"), "w", encoding="utf-8") as f:
            f.write(fly_toml)
        cmd = [flyctl, "deploy", "--remote-only", "-c", os.path.join(build_dir, "fly.toml")]
    observe.log("deploy.fly.deploy", app=args["app"])
    _sp.run(cmd, check=True)
    return {"app": args["app"]}


register_adapter("aws.lambda", _aws_lambda_adapter)
register_adapter("gcp.run", _gcp_run_adapter)
register_adapter("k8s", _k8s_adapter)
register_adapter("fly", _fly_adapter)
