"""Daemon loop for `c4 serve`.

Drives the registries populated at module import time:

- ``every Ns { ... }`` callbacks run on a single scheduler thread.
- ``on event from SOURCE { ... }`` callbacks subscribe to a source object
  exposing ``.poll(timeout=...) -> Iterable[event]``.
- ``serve handler on :port`` boots a lightweight HTTP server (one per port).

Sources for queues / event streams are expected to follow a tiny protocol:

    class EventSource:
        def poll(self, timeout: float) -> Iterable[Any]: ...
        def close(self) -> None: ...

A ``InMemoryQueue`` implementation lives in this module for testing.
"""

from __future__ import annotations

import json
import os
import queue as stdlib_queue
import signal
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterable

from cobra4.runtime import core as core_rt
from cobra4.runtime import observe

# ---------- In-memory queue (for tests / simple cases) ----------


class InMemoryQueue:
    def __init__(self, name: str = "anon") -> None:
        self.name = name
        self._q: stdlib_queue.Queue = stdlib_queue.Queue()
        self._closed = False

    def put(self, event: Any) -> None:
        if self._closed:
            raise RuntimeError("queue is closed")
        self._q.put(event)

    def poll(self, timeout: float = 0.5) -> Iterable[Any]:
        try:
            yield self._q.get(timeout=timeout)
        except stdlib_queue.Empty:
            return

    def close(self) -> None:
        self._closed = True


_queues: dict[str, "QueueLike"] = {}


class QueueLike:
    """Protocol: any object with ``.put()``, ``.poll(timeout)``, ``.close()``."""

    name: str

    def put(self, event):  # pragma: no cover - protocol
        raise NotImplementedError

    def poll(self, timeout: float = 0.5):  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass


# ---------- File-backed queue (durable across restarts) ----------


class FileQueue:
    """Simple durable queue backed by a directory of JSON files.

    Each ``put()`` writes a new file ``<dir>/<ts>_<seq>.json``.
    ``poll()`` reads + unlinks the oldest file. Ordering is by filename
    (timestamp prefix). Safe for single-consumer use.

    Use it for local dev or as a "dead simple" queue when you don't want
    a broker.
    """

    def __init__(self, name: str, root: str | os.PathLike | None = None) -> None:
        import os as _os
        from pathlib import Path

        self.name = name
        base = root or _os.environ.get("COBRA4_FILE_QUEUE_DIR") or "./.cobra4/queues"
        self.dir = Path(base) / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0
        self._lock = threading.Lock()

    def put(self, event) -> None:
        import time as _t

        with self._lock:
            self._seq = (self._seq + 1) % 1_000_000
            fname = f"{int(_t.time() * 1000):015d}_{self._seq:06d}.json"
            tmp = self.dir / (fname + ".tmp")
            tmp.write_text(json.dumps(event, default=str), encoding="utf-8")
            (tmp).rename(self.dir / fname)

    def poll(self, timeout: float = 0.5):
        import time as _t

        end = _t.monotonic() + timeout
        while _t.monotonic() < end:
            files = sorted(p for p in self.dir.iterdir() if p.suffix == ".json")
            if files:
                p = files[0]
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    p.unlink()
                    yield data
                    return
                except (FileNotFoundError, OSError):
                    continue
            _t.sleep(0.05)

    def close(self) -> None:
        pass


# ---------- SQS queue ----------


class SQSQueue:
    """AWS SQS-backed queue. Requires ``cobra4[aws]`` and AWS credentials.

    Pass either a queue URL or a queue name (the URL is resolved via
    ``get_queue_url``). Ack semantics: messages are *deleted on poll
    success*, so a handler crash after yield but before processing
    completion would lose the event. For at-least-once, consume the
    raw ``ReceiptHandle`` and call ``client.delete_message`` yourself.
    """

    def __init__(self, name_or_url: str, *, region: str | None = None) -> None:
        try:
            import boto3  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("SQSQueue requires `cobra4[aws]`") from e
        self.name = name_or_url
        self._client = boto3.client("sqs", region_name=region)
        if name_or_url.startswith("https://"):
            self.url = name_or_url
        else:
            self.url = self._client.get_queue_url(QueueName=name_or_url)["QueueUrl"]

    def put(self, event) -> None:
        body = event if isinstance(event, str) else json.dumps(event, default=str)
        self._client.send_message(QueueUrl=self.url, MessageBody=body)

    def poll(self, timeout: float = 0.5):
        wait = max(0, min(20, int(timeout)))  # SQS hard limit
        resp = self._client.receive_message(
            QueueUrl=self.url, MaxNumberOfMessages=10, WaitTimeSeconds=wait
        )
        for m in resp.get("Messages", []):
            try:
                yield json.loads(m["Body"])
            except (json.JSONDecodeError, ValueError):
                yield m["Body"]
            self._client.delete_message(
                QueueUrl=self.url, ReceiptHandle=m["ReceiptHandle"]
            )

    def close(self) -> None:
        pass


# ---------- Redis queue (LIST-based) ----------


class RedisQueue:
    """Redis LIST-based queue.

    ``put`` → ``LPUSH``, ``poll`` → ``BRPOP`` with timeout. Requires
    ``pip install redis``. Connection via ``redis://[user:pass@]host:port/db``.
    """

    def __init__(self, key: str, *, url: str | None = None) -> None:
        try:
            import redis  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("RedisQueue requires `pip install redis`") from e
        self.name = key
        url = url or os.environ.get("COBRA4_REDIS_URL", "redis://localhost:6379/0")
        self._r = redis.Redis.from_url(url, decode_responses=True)
        self._key = key

    def put(self, event) -> None:
        body = event if isinstance(event, str) else json.dumps(event, default=str)
        self._r.lpush(self._key, body)

    def poll(self, timeout: float = 0.5):
        # BRPOP wait must be int seconds; cap at 1s for responsiveness.
        wait = max(1, int(timeout))
        result = self._r.brpop([self._key], timeout=wait)
        if result is None:
            return
        _, body = result
        try:
            yield json.loads(body)
        except (json.JSONDecodeError, ValueError):
            yield body

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass


def queue(name: str, *, kind: str | None = None, **kwargs) -> QueueLike:
    """Get-or-create a queue. Backend selection in priority order:

    1. Explicit ``kind=`` arg (``"memory"``, ``"file"``, ``"sqs"``, ``"redis"``).
    2. ``COBRA4_QUEUE_BACKEND`` env var (same values).
    3. Default: ``"memory"``.

    Caches by name so repeated calls return the same instance.
    """
    from cobra4.runtime.effects import check as _check_effect

    _check_effect("time")
    if name in _queues:
        return _queues[name]

    backend = kind or os.environ.get("COBRA4_QUEUE_BACKEND", "memory")
    if backend == "file":
        q: QueueLike = FileQueue(name, **kwargs)
    elif backend == "sqs":
        q = SQSQueue(name, **kwargs)
    elif backend == "redis":
        q = RedisQueue(name, **kwargs)
    elif backend == "memory":
        q = InMemoryQueue(name)
    else:
        raise ValueError(f"unknown queue backend '{backend}'")
    _queues[name] = q
    return q


# ---------- HTTP handler ----------


@dataclass
class _Request:
    """HTTP request as seen by a cobra4 handler.

    - ``method`` / ``path`` are strings.
    - ``params`` collapses query-string lists: single values become the
      string itself, multi-valued keys keep the list.
    - ``headers`` is lower-cased (HTTP headers are case-insensitive).
    - ``body`` is raw bytes; use ``.json()`` to decode JSON.
    """

    method: str
    path: str
    params: dict
    headers: dict
    body: bytes

    def json(self):
        """Decode the request body as JSON. Returns ``None`` for empty body."""
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        """Decode the body as UTF-8 text."""
        return self.body.decode("utf-8") if self.body else ""


def _encode_response(result: Any) -> tuple[int, dict[str, str], bytes]:
    """Turn a handler return value into ``(status, headers, body_bytes)``.

    Conventions:

    - ``(status, headers, body)`` tuple → used as-is. ``body`` may be
      bytes or str; str is utf-8 encoded.
    - ``(status, body)`` two-tuple → status + auto-typed body.
    - ``bytes`` → ``200 OK`` ``application/octet-stream``.
    - ``str`` → ``200 OK`` ``text/plain; charset=utf-8`` (use
      ``("text/html", body)`` if you want HTML).
    - ``dict`` / ``list`` → ``200 OK`` ``application/json``.
    - anything else → JSON-encoded.
    """
    status = 200
    body: bytes = b""
    headers: dict[str, str] = {}

    if isinstance(result, tuple):
        if (
            len(result) == 3
            and isinstance(result[0], int)
            and isinstance(result[1], dict)
        ):
            status, headers, payload = result
            body = (
                payload.encode("utf-8")
                if isinstance(payload, str)
                else (payload or b"")
            )
        elif len(result) == 2 and isinstance(result[0], int):
            status, payload = result
            return _encode_response(payload)[:1] + (_encode_response(payload)[1], _encode_response(payload)[2])  # type: ignore[index]
        else:
            return _encode_response(list(result))
    elif isinstance(result, bytes):
        body = result
        headers.setdefault("content-type", "application/octet-stream")
    elif isinstance(result, str):
        body = result.encode("utf-8")
        headers.setdefault("content-type", "text/plain; charset=utf-8")
    elif result is None:
        body = b""
    else:
        body = json.dumps(result, default=str).encode("utf-8")
        headers.setdefault("content-type", "application/json")
    headers.setdefault("content-length", str(len(body)))
    return status, headers, body


class _HandlerAdapter(BaseHTTPRequestHandler):
    handler_fn: Callable[..., Any] = None  # type: ignore[assignment]

    def _serve(self):
        from urllib.parse import urlparse, parse_qs

        url = urlparse(self.path)
        params = {k: v[0] if len(v) == 1 else v for k, v in parse_qs(url.query).items()}
        body = b""
        if self.headers.get("content-length"):
            try:
                body = self.rfile.read(int(self.headers["content-length"]))
            except ValueError:
                body = b""
        req = _Request(
            method=self.command,
            path=url.path,
            params=params,
            headers={k.lower(): v for k, v in self.headers.items()},
            body=body,
        )
        try:
            result = self.handler_fn(req)
            status, headers, payload = _encode_response(result)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(payload)
        except Exception as e:  # noqa: BLE001
            err = json.dumps({"error": str(e), "type": type(e).__name__}).encode(
                "utf-8"
            )
            self.send_response(500)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(err)))
            self.end_headers()
            self.wfile.write(err)

    def log_message(
        self, format: str, *args: Any
    ) -> None:  # quiet down BaseHTTPRequestHandler
        observe.log(
            "http",
            method=self.command,
            path=self.path,
            status=args[1] if len(args) > 1 else "?",
        )

    do_GET = _serve
    do_POST = _serve
    do_PUT = _serve
    do_DELETE = _serve
    do_PATCH = _serve
    do_HEAD = _serve
    do_OPTIONS = _serve


# ---------- Daemon ----------


@dataclass
class _DaemonState:
    stop_event: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)
    servers: list[ThreadingHTTPServer] = field(default_factory=list)


def _scheduler_loop(state: _DaemonState) -> None:
    deadlines: dict[int, float] = {}
    entries = list(core_rt.schedule_registry())
    now = time.monotonic()
    for i, _e in enumerate(entries):
        deadlines[i] = now  # fire immediately on start
    while not state.stop_event.is_set():
        now = time.monotonic()
        for i, e in enumerate(entries):
            if now >= deadlines[i]:
                try:
                    e.fn()
                except Exception as exc:  # noqa: BLE001
                    observe.log.error("scheduler.fn failed", error=str(exc))
                deadlines[i] = now + e.seconds
        time.sleep(0.05)


def _event_loop(state: _DaemonState) -> None:
    entries = list(core_rt.event_registry())
    if not entries:
        return
    while not state.stop_event.is_set():
        for e in entries:
            src = e.source
            if not hasattr(src, "poll"):
                continue
            try:
                for event in src.poll(timeout=0.2):
                    e.fn(event)
            except Exception as exc:  # noqa: BLE001
                observe.log.error("event source poll failed", error=str(exc))
        time.sleep(0.05)


def _start_http_servers(state: _DaemonState) -> None:
    """Start one HTTP server per ``serve handler on :PORT`` registration.

    Bind address default: ``127.0.0.1`` (loopback only — safer for the
    common "running locally" case). Override with ``COBRA4_HTTP_BIND``,
    e.g. ``COBRA4_HTTP_BIND=0.0.0.0`` to expose on all interfaces.
    """
    import os as _os

    bind_addr = _os.environ.get("COBRA4_HTTP_BIND", "127.0.0.1")
    for entry in core_rt.serve_registry():
        port = entry.port
        handler = entry.handler
        cls = type(
            f"_C4Handler_{port}",
            (_HandlerAdapter,),
            {"handler_fn": staticmethod(handler)},
        )
        server = ThreadingHTTPServer((bind_addr, port), cls)
        state.servers.append(server)
        t = threading.Thread(
            target=server.serve_forever, name=f"c4-http-{port}", daemon=True
        )
        t.start()
        state.threads.append(t)
        observe.log("serve.bound", host=bind_addr, port=port)


def serve_forever(timeout: float | None = None) -> None:
    """Boot scheduler + event loop + HTTP servers, block until interrupted.

    ``timeout`` is for tests: stop after N seconds even without signal.
    """
    state = _DaemonState()

    if core_rt.schedule_registry():
        t = threading.Thread(
            target=_scheduler_loop, args=(state,), name="c4-scheduler", daemon=True
        )
        t.start()
        state.threads.append(t)
    if core_rt.event_registry():
        t = threading.Thread(
            target=_event_loop, args=(state,), name="c4-events", daemon=True
        )
        t.start()
        state.threads.append(t)
    _start_http_servers(state)

    def _shutdown(*_a: Any) -> None:
        observe.log("daemon.stop")
        state.stop_event.set()
        for srv in state.servers:
            srv.shutdown()

    try:
        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)
    except (ValueError, OSError):
        # not in main thread (tests) — fine
        pass

    observe.log(
        "daemon.start",
        scheduled=len(core_rt.schedule_registry()),
        events=len(core_rt.event_registry()),
        http_ports=len(state.servers),
    )

    if timeout is not None:
        state.stop_event.wait(timeout)
        _shutdown()
    else:
        # idle until SIGINT/SIGTERM
        try:
            while not state.stop_event.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            _shutdown()
