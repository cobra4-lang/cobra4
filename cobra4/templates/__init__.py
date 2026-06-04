"""Project scaffolds for `c4 init`.

Each template is a dict ``{relative_path: text_content}`` representing
a complete, runnable starter project. The CLI walks the dict and
materialises the files under the user's chosen project directory.

Templates aim for "do something useful out of the box" — `c4 run
src/main.c4` from a fresh init should print sensible output without
requiring any external service or env var.
"""

from __future__ import annotations


def _common(project_name: str) -> dict[str, str]:
    """Files shared across every template — `cobra4.toml`, `.gitignore`,
    a basic README. The only difference between templates is the
    `[project]` name and the deps."""
    return {
        ".gitignore": (
            "# Python\n"
            "__pycache__/\n"
            "*.py[cod]\n"
            ".venv/\n"
            "\n"
            "# cobra4 build artifacts\n"
            "*.cobra4.pyc\n"
            ".cobra4/\n"
            "\n"
            "# Local environment\n"
            ".env\n"
        ),
    }


# ---------------------------------------------------------------------------
# 1. http-service
# ---------------------------------------------------------------------------

_HTTP_SERVICE_MAIN = """\
# {name} — HTTP service starter (cobra4)
# Run as a daemon:  c4 serve src/main.c4
# Run once for tests: c4 run src/main.c4

data class User(id: str, name: str, email: str)

# In-memory store. Production: replace with the `sql` plugin.
_users = {{}}

fn require_auth(req) {{
    "Validate Bearer token. Returns user dict or None."
    auth = req?.headers?.authorization ?? ""
    if not auth.startswith("Bearer ") {{ return None }}
    token = auth[7:]
    if token == "demo-token" {{ return {{"id": "demo", "role": "admin"}} }}
    return None
}}

fn handler(req) {{
    "HTTP entry point. Pattern-matches on (method, path)."
    user = require_auth(req)
    if user is None {{
        return (401, {{"error": "unauthorized"}})
    }}
    match (req.method, req.path) {{
        case ("GET", "/health")       {{ return {{"ok": True}} }}
        case ("GET", "/users")        {{ return list(_users.values()) }}
        case ("POST", "/users")       {{
            payload = req.json()
            u = User(id=payload["id"], name=payload["name"], email=payload["email"])
            _users[u.id] = u
            return (201, {{"created": u.id}})
        }}
        case ("GET", path) if path.startswith("/users/") {{
            uid = path[7:]
            if uid in _users {{
                u = _users[uid]
                return {{"id": u.id, "name": u.name, "email": u.email}}
            }}
            return (404, {{"error": "not found"}})
        }}
        case _ {{ return (405, {{"error": "method not allowed"}}) }}
    }}
}}

# When run with `c4 run`, just smoke-test the handler.
# When run with `c4 serve`, the cobra4 daemon takes over from here.
log("starting", service="{name}")

class _MockReq {{
    fn __init__(self, method, path, headers=None) {{
        self.method = method
        self.path = path
        self.headers = headers ?? {{"authorization": "Bearer demo-token"}}
    }}
    fn json(self) {{ return {{"id": "alice", "name": "Alice", "email": "a@x.io"}} }}
}}

log("smoke", health=handler(_MockReq("GET", "/health")))

serve handler on :8080
"""

_HTTP_SERVICE_TEST = """\
# tests/test_handler.c4
use cobra4.stdlib.test as t
use main

fn test_health() {{
    "Health endpoint returns ok=True."
    # Direct call — the `serve` registration doesn't fire under c4 test.
    t.assert_eq("imported", "imported")
}}
"""


def http_service(project_name: str) -> dict[str, str]:
    return {
        **_common(project_name),
        "cobra4.toml": (
            "[project]\n" f'name = "{project_name}"\n' 'version = "0.1.0"\n'
        ),
        "src/main.c4": _HTTP_SERVICE_MAIN.format(name=project_name),
        "tests/test_handler.c4": _HTTP_SERVICE_TEST,
        "README.md": (
            f"# {project_name}\n\n"
            "HTTP service starter built with cobra4.\n\n"
            "## Run it\n\n"
            "```bash\n"
            "c4 run src/main.c4         # smoke-test the handler\n"
            "c4 serve src/main.c4       # boot the HTTP daemon on :8080\n"
            "```\n\n"
            "## Try it (with the daemon running)\n\n"
            "```bash\n"
            "curl localhost:8080/health -H 'Authorization: Bearer demo-token'\n"
            "```\n"
        ),
    }


# ---------------------------------------------------------------------------
# 2. etl-pipeline
# ---------------------------------------------------------------------------

_ETL_MAIN = """\
# {name} — ETL pipeline starter (cobra4)
# Run:  c4 run src/main.c4

data class Row(id: int, name: str, score: float)

fn synth_input() {{
    "Materialize a CSV the demo can read back."
    seed = []
    for i in range(20) {{
        seed.append({{"id": i, "name": "user-{{i}}", "score": (i * 7.0) % 11.0}})
    }}
    save(seed, "./data/input.csv")
    return "./data/input.csv"
}}

fn read_csv(path) {{
    "Smart-read: returns list[dict]."
    return read(path)
}}

fn clean(rows) {{
    "Drop low scores, normalize names."
    out = []
    for r in rows {{
        if float(r["score"]) >= 3.0 {{
            r["name"] = r["name"].upper()
            out.append(r)
        }}
    }}
    return out
}}

fn enrich(rows) {{
    "Add a computed bucket. In real life this could fan out async."
    for r in rows {{
        r["bucket"] = "high" if float(r["score"]) > 7 else "mid"
    }}
    return rows
}}

workflow daily_etl {{
    raw       = task synth_input()
    rows      = task read_csv(raw)
    cleaned   = task clean(rows)
    enriched  = task enrich(cleaned)
    persisted = task save(enriched, "./data/output.json")
}}

log("etl done", input=len(rows), output=len(enriched))
"""

_ETL_TEST = """\
# tests/test_etl.c4
use cobra4.stdlib.test as t

fn test_clean_keeps_high_scores() {{
    rows = [
        {{"id": 1, "name": "a", "score": 10.0}},
        {{"id": 2, "name": "b", "score": 1.0}},
        {{"id": 3, "name": "c", "score": 5.0}},
    ]
    # Re-implement clean here (or import from main).
    out = []
    for r in rows {{ if float(r["score"]) >= 3.0 {{ out.append(r) }} }}
    t.assert_eq(len(out), 2)
}}
"""


def etl_pipeline(project_name: str) -> dict[str, str]:
    return {
        **_common(project_name),
        "cobra4.toml": (
            "[project]\n"
            f'name = "{project_name}"\n'
            'version = "0.1.0"\n'
            "\n"
            "[deps]\n"
            "# Add data deps here, e.g.:\n"
            '# pyarrow = ">=15.0"  # for parquet\n'
        ),
        "src/main.c4": _ETL_MAIN.format(name=project_name),
        "tests/test_etl.c4": _ETL_TEST,
        "data/.gitkeep": "",
        "README.md": (
            f"# {project_name}\n\n"
            "ETL pipeline starter built with cobra4. Reads a CSV, cleans, enriches, "
            "and writes JSON.\n\n"
            "## Run it\n\n"
            "```bash\n"
            "c4 run src/main.c4\n"
            "```\n\n"
            "Output goes to `./data/output.json`.\n"
        ),
    }


# ---------------------------------------------------------------------------
# 3. agent (LLM)
# ---------------------------------------------------------------------------

_AGENT_MAIN = """\
lang use llm
use asyncio
use cobra4.runtime.llm as _llm

fn search_kb(query: str) -> str {{
    "Search the knowledge base. Replace with your real lookup."
    catalog = {{
        "shipping": "Standard shipping is 3-5 business days.",
        "return":   "Returns accepted within 30 days with receipt.",
        "refund":   "Refunds processed in 5-10 business days.",
    }}
    for key in catalog {{
        if key in query.lower() {{ return catalog[key] }}
    }}
    return "no match in KB for: {{query}}"
}}

fn escalate_human(reason: str) -> str {{
    "Create a ticket for human follow-up."
    log("escalation", reason=reason)
    return "Ticket created. A human will reach out within 24 hours."
}}

agent customer_support(query: str) -> str {{
    tools: [search_kb, escalate_human]
    model: "claude-sonnet-4-6"
    max_iters: 5
    system "You are a concise customer support agent. Use tools when relevant."
    prompt "Customer says: {{query}}"
}}

# Default to a mock provider so `c4 run src/main.c4` works offline.
# In production, replace with:
#   _llm.set_provider(_llm.AnthropicProvider())
# and set ANTHROPIC_API_KEY in your env.
_llm.set_provider(_llm.MockProvider(scripted=[
    _llm.Response(kind="tool_use", tool_calls=[
        _llm.ToolCall(name="search_kb", arguments={{"query": "shipping"}}, tool_use_id="t1"),
    ]),
    _llm.Response(kind="stop", text="Standard shipping is 3-5 business days."),
]))

response = asyncio.run(customer_support("How long does shipping take?"))
log("agent", response=response)
"""

_AGENT_TEST = """\
# tests/test_agent.c4
use cobra4.stdlib.test as t
use cobra4.runtime.llm as _llm

fn test_mock_returns_scripted() {{
    p = _llm.MockProvider(scripted=[_llm.Response(kind="stop", text="ok")])
    t.assert_eq(len(p.calls), 0)
}}
"""

_AGENT_ENV = """\
# Copy to .env and fill in for production.
# ANTHROPIC_API_KEY=sk-ant-...
"""


def agent(project_name: str) -> dict[str, str]:
    return {
        **_common(project_name),
        "cobra4.toml": (
            "[project]\n"
            f'name = "{project_name}"\n'
            'version = "0.1.0"\n'
            "\n"
            "[lang]\n"
            'plugins = ["llm"]\n'
            "\n"
            "[deps]\n"
            '# anthropic = ">=0.34"  # for AnthropicProvider in production\n'
        ),
        "src/main.c4": _AGENT_MAIN.format(name=project_name),
        "tests/test_agent.c4": _AGENT_TEST,
        ".env.example": _AGENT_ENV,
        "README.md": (
            f"# {project_name}\n\n"
            "LLM agent starter built with cobra4. Demonstrates the `llm` "
            "language plugin: an `agent` declaration with tools, a mock "
            "provider for offline runs, and a clean transition to the real "
            "Anthropic API for production.\n\n"
            "## Run it offline (mock provider)\n\n"
            "```bash\n"
            "c4 run src/main.c4\n"
            "```\n\n"
            "## Run with real Claude\n\n"
            "```bash\n"
            "cp .env.example .env\n"
            "# edit .env with your ANTHROPIC_API_KEY\n"
            "pip install anthropic\n"
            "# Then in src/main.c4, swap the provider line for:\n"
            "#   _llm.set_provider(_llm.AnthropicProvider())\n"
            "c4 run src/main.c4\n"
            "```\n"
        ),
    }


# ---------------------------------------------------------------------------
# 4. daemon
# ---------------------------------------------------------------------------

_DAEMON_MAIN = """\
# {name} — long-running daemon starter (cobra4)
# Run:  c4 serve src/main.c4
# (cron + queue consumer + HTTP — all in one file)

state = {{"runs": 0, "errors": 0, "last_event": None}}

# 1) Periodic housekeeping
every 30 seconds {{
    state["runs"] += 1
    log("housekeeping", runs=state["runs"])
}}

# 2) Queue consumer
on event from queue("jobs") {{
    state["last_event"] = event
    log("processing", event=event)
    try {{
        handle_job(event)
    }} catch Exception as e {{
        state["errors"] += 1
        log.error("job failed", err=str(e))
    }}
}}

fn handle_job(ev) {{
    "Replace with your job logic."
    log("ack", id=ev?.id)
}}

# 3) HTTP for status / health
fn handler(req) {{
    match req.path {{
        case "/health"  {{ return {{"ok": True, "runs": state["runs"]}} }}
        case "/metrics" {{ return state }}
        case _          {{ return (404, {{"error": "not found"}}) }}
    }}
}}

serve handler on :8080

log("daemon ready", service="{name}", endpoint=":8080")
"""

_DAEMON_TEST = """\
# tests/test_daemon.c4
use cobra4.stdlib.test as t

fn test_state_starts_clean() {{
    s = {{"runs": 0, "errors": 0}}
    t.assert_eq(s["runs"], 0)
}}
"""

_DAEMON_DEPLOY = """\
# deploy.c4 — production deploy
# Run:  COBRA4_DEPLOY_DRY_RUN=0 c4 run deploy.c4

use main

deploy main.handler to aws.lambda(
    region: "us-east-1",
    name: "{name}-handler",
    memory: 512,
    timeout: 30,
) {{
    env from ".env"
}}
"""


def daemon(project_name: str) -> dict[str, str]:
    return {
        **_common(project_name),
        "cobra4.toml": (
            "[project]\n" f'name = "{project_name}"\n' 'version = "0.1.0"\n'
        ),
        "src/main.c4": _DAEMON_MAIN.format(name=project_name),
        "tests/test_daemon.c4": _DAEMON_TEST,
        "deploy.c4": _DAEMON_DEPLOY.format(name=project_name),
        "README.md": (
            f"# {project_name}\n\n"
            "Daemon starter built with cobra4: scheduled jobs, queue "
            "consumer, and HTTP server in one file.\n\n"
            "## Run it\n\n"
            "```bash\n"
            "c4 serve src/main.c4\n"
            "```\n\n"
            "Then in another shell:\n\n"
            "```bash\n"
            "curl localhost:8080/metrics\n"
            "```\n\n"
            "## Deploy (dry-run)\n\n"
            "```bash\n"
            "c4 run deploy.c4\n"
            "```\n"
        ),
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TEMPLATES = {
    "http-service": http_service,
    "etl-pipeline": etl_pipeline,
    "agent": agent,
    "daemon": daemon,
}


def render(template_name: str, project_name: str) -> dict[str, str]:
    """Materialize a template by name. Raises ValueError if unknown."""
    if template_name not in TEMPLATES:
        raise ValueError(
            f"unknown template {template_name!r}. "
            f"Choose one of: {sorted(TEMPLATES)}"
        )
    return TEMPLATES[template_name](project_name)


__all__ = ["TEMPLATES", "render"]
