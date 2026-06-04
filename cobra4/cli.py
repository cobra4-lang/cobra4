"""cobra4 CLI — ``c4`` and ``cobra4`` commands.

Sub-commands implemented in M1:

- ``run FILE``   — transpile and execute a .c4 file.
- ``build FILE`` — transpile only; write Python to ``-o OUT`` (default
                   stdout).
- ``fmt FILE``   — apply the canonical formatter (M1: parse → re-emit
                   cobra4 from AST is out of scope; we only normalize
                   trailing whitespace and run the parser to validate).
- ``repl``       — interactive expression evaluator.

Stubs (real implementation lands in later milestones):

- ``serve``, ``check``, ``doc``, ``deps``, ``plugin``.
"""

from __future__ import annotations

import argparse
import os
import runpy
import sys
import tempfile
import textwrap
import traceback
from pathlib import Path
from typing import Optional

from cobra4 import __version__
from cobra4.parser import parse, ParseError
from cobra4.resolver import resolve
from cobra4.typecheck import check as typecheck
from cobra4.dispatch_analysis import analyze as dispatch_analyze
from cobra4.lowering import lower
from cobra4.codegen import generate
from cobra4.plugins import preprocess, list_plugins as plugin_list
from cobra4.plugins.loader import preserve_plugin_constructs


def _compile_file(path: Path) -> tuple[str, "object"]:
    src = path.read_text(encoding="utf-8")
    try:
        pre = preprocess(src)
    except ValueError as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(2)
    try:
        module = parse(pre.source, source_path=str(path))
    except ParseError as e:
        sys.stderr.write(str(e) + "\n")
        sys.exit(2)
    # Only fail on errors; warnings flow to stderr but don't block run/build.
    rr = resolve(module)
    for d in rr.errors:
        sys.stderr.write(f"{d}\n")
    if not rr.ok():
        sys.exit(2)
    module = lower(module)
    result = generate(module, cobra4_path=str(path))
    code = result.code
    # Inject plugin runtime imports if any plugin needs them.
    if pre.plugins:
        plugin_imports = "\n".join(
            f"from {p.runtime_module} import *  # noqa: F401,F403  (plugin: {p.name})"
            for p in pre.plugins
            if p.runtime_module
        )
        if plugin_imports:
            code = code.replace(
                "# DO NOT EDIT",
                plugin_imports + "\n# DO NOT EDIT",
                1,
            )
    return code, result.source_map


def cmd_build(args: argparse.Namespace) -> int:
    path = Path(args.file)
    code, smap = _compile_file(path)
    if args.output and args.output != "-":
        Path(args.output).write_text(code, encoding="utf-8")
        if args.source_map:
            Path(args.output + ".pymap").write_text(smap.serialize(), encoding="utf-8")
    else:
        sys.stdout.write(code)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    path = Path(args.file)
    code, smap = _compile_file(path)
    program_args = list(getattr(args, "program_args", []) or [])
    # Write generated module to a temp file so tracebacks can point to a
    # real location; we still rewrite frames via source_map for clarity.
    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix=f"{path.stem}__c4_",
        mode="w",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        # Make sure the project package is importable (for `from cobra4.runtime import ...`).
        proj_root = Path(__file__).resolve().parent.parent
        if str(proj_root) not in sys.path:
            sys.path.insert(0, str(proj_root))
        old_argv = sys.argv[:]
        sys.argv = [str(path), *program_args]
        ns = {
            "__name__": "__main__",
            "__file__": tmp_path,
            "__package__": None,
            "__cached__": None,
        }
        exec(compile(Path(tmp_path).read_text(encoding="utf-8"), tmp_path, "exec"), ns)
    except SystemExit:
        raise
    except BaseException:
        _print_traceback_with_source_map(tmp_path, smap, str(path))
        return 1
    finally:
        if "old_argv" in locals():
            sys.argv = old_argv
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
    return 0


def _print_traceback_with_source_map(py_path: str, smap, c4_path: str) -> None:
    """Rewrite Python tracebacks to point at the original cobra4 source.

    Three improvements over plain ``traceback.format_exc()``:
    1. Frames inside the temp .py get their file/line replaced with the
       corresponding ``.c4:line:col`` and the *cobra4* source code line
       (not the transpiled Python).
    2. Frames inside cobra4's own machinery (cli.py, runpy, this
       function) are dropped — they're never the user's bug. Set
       ``COBRA4_TRACE_VERBOSE=1`` to keep them.
    3. The ``(transpiled from ...)`` annotation is suppressed by
       default; only shown in verbose mode for debugging the compiler.
    """
    import os as _os

    verbose = _os.environ.get("COBRA4_TRACE_VERBOSE") == "1"

    # Read the cobra4 source once so we can show the actual c4 line text.
    try:
        c4_lines = Path(c4_path).read_text().splitlines()
    except OSError:
        c4_lines = []

    # `format_exc` produces our paragraph; we walk it line-by-line and
    # rewrite. Each frame is two lines: a `File "..."` header and a
    # source-text line.
    raw = traceback.format_exc().splitlines()
    out: list[str] = []
    i = 0

    def _is_internal_frame_header(ln: str) -> bool:
        return ln.startswith("  File ") and (
            "/cli.py" in ln
            or "<frozen runpy>" in ln
            or "_print_traceback_with_source_map" in ln
        )

    def _is_frame_header(ln: str) -> bool:
        return ln.startswith("  File ")

    while i < len(raw):
        ln = raw[i]

        # If we hit an internal frame header, skip it AND all of its
        # body lines (source line + optional caret markers) until the
        # next frame header (or the final exception line).
        if not verbose and _is_internal_frame_header(ln):
            i += 1
            while (
                i < len(raw) and not _is_frame_header(raw[i]) and raw[i].startswith(" ")
            ):
                i += 1
            continue

        # Rewrite a user frame that points into our temp .py file.
        if py_path in ln and ", line " in ln:
            try:
                left, rest = ln.split(", line ", 1)
                num_str, *tail = rest.split(",", 1)
                py_line = int(num_str)
                c4_line, c4_col = smap.lookup_position(py_line, 0)
                if c4_line:
                    pos = f"{c4_line}" if c4_col == 0 else f"{c4_line}:{c4_col}"
                    suffix = (
                        f" (transpiled from {Path(py_path).name}:{py_line})"
                        if verbose
                        else ""
                    )
                    new_ln = f'  File "{c4_path}", line {pos}'
                    if tail:
                        new_ln += "," + tail[0]
                    new_ln += suffix
                    out.append(new_ln)
                    # Replace this frame's body (source line + optional
                    # caret markers) with the actual cobra4 source.
                    if 0 < c4_line <= len(c4_lines):
                        out.append("    " + c4_lines[c4_line - 1].strip())
                    i += 1
                    while (
                        i < len(raw)
                        and not _is_frame_header(raw[i])
                        and raw[i].startswith(" ")
                    ):
                        i += 1
                    continue
            except Exception:
                pass

        out.append(ln)
        i += 1

    sys.stderr.write("\n".join(out) + "\n")


def cmd_fmt(args: argparse.Namespace) -> int:
    """Canonical formatter: parse → re-emit from AST.

    Plugin-aware: ``lang use NAME`` directives and plugin-specific
    constructs (``sql { ... }``, ``re"..."`` literals, etc.) are
    preserved verbatim — the formatter only reformats the surrounding
    cobra4 code.

    Idempotent: running ``c4 fmt`` twice produces the same output.
    """
    from cobra4.tools.fmt import format_module

    path = Path(args.file)
    src = path.read_text(encoding="utf-8")

    # Capture lang use directives so we can re-prepend them.
    directives = []
    body_lines = []
    seen_body = False
    for line in src.splitlines(keepends=True):
        stripped = line.strip()
        if not seen_body and (
            stripped.startswith("lang use ")
            or not stripped
            or stripped.startswith("#")
            or stripped.startswith("//")
        ):
            if stripped.startswith("lang use "):
                directives.append(stripped)
            elif not stripped:
                # blank line before body — drop
                continue
        else:
            seen_body = True
        if seen_body:
            body_lines.append(line)
    body = "".join(body_lines) if body_lines else src

    try:
        sentinel_body, restorers, _plugins = preserve_plugin_constructs(src)
        module = parse(sentinel_body, source_path=str(path))
    except (ParseError, ValueError) as e:
        sys.stderr.write(str(e) + "\n")
        return 2

    formatted = format_module(module)
    # Restore plugin constructs verbatim.
    for sentinel, original in restorers:
        # placeholder may be emitted as `_C4_X_0` (Name) or `_C4_X_0()` (Call).
        formatted = formatted.replace(sentinel + "()", original)
        formatted = formatted.replace(sentinel, original)
    # Re-prepend lang use directives at the top.
    if directives:
        formatted = "\n".join(directives) + "\n\n" + formatted

    if args.write:
        path.write_text(formatted, encoding="utf-8")
    else:
        sys.stdout.write(formatted)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.file)
    src = path.read_text(encoding="utf-8")
    try:
        pre = preprocess(src)
    except ValueError as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    try:
        module = parse(pre.source, source_path=str(path))
    except ParseError as e:
        sys.stderr.write(str(e) + "\n")
        return 2

    rr = resolve(
        module,
        warn_undefined=not args.no_undefined,
        warn_shadowing=not args.no_shadowing,
        extra_builtins=pre.extra_builtins,
    )
    type_diags = typecheck(module) if not args.no_types else []
    disp_diags = dispatch_analyze(module) if not args.no_dispatch else []

    all_diags = list(rr.diagnostics) + type_diags + disp_diags
    errors = [d for d in all_diags if d.severity == "error"]
    warnings = [d for d in all_diags if d.severity == "warning"]

    for d in all_diags:
        sys.stderr.write(f"{d}\n")

    if errors:
        sys.stderr.write(
            f"{path}: {len(errors)} error(s), {len(warnings)} warning(s)\n"
        )
        return 2
    if warnings and args.strict:
        sys.stderr.write(f"{path}: {len(warnings)} warning(s) (strict mode)\n")
        return 2
    sys.stdout.write(f"{path}: OK ({len(warnings)} warning(s))\n")
    return 0


def cmd_repl(args: argparse.Namespace) -> int:  # noqa: ARG001
    """Multi-line REPL with auto-continuation on unbalanced braces."""
    from cobra4.tools.repl import run_repl

    return run_repl()


def cmd_idle(args: argparse.Namespace) -> int:
    """Run the browser-based cobra4 IDLE."""
    from cobra4.idle import serve as serve_idle

    return serve_idle(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        verbose=args.verbose,
    )


def cmd_serve(args: argparse.Namespace) -> int:
    """Run a .c4 file as a long-lived daemon.

    The module is imported once (registering ``every`` / ``on event`` /
    ``serve`` callbacks); then the runtime takes over the loop.
    """
    from cobra4.runtime.schedule import serve_forever
    from cobra4.runtime.core import reset_registries

    reset_registries()  # fresh per invocation
    path = Path(args.file)
    code, smap = _compile_file(path)
    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix=f"{path.stem}__c4serve_",
        mode="w",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        proj_root = Path(__file__).resolve().parent.parent
        if str(proj_root) not in sys.path:
            sys.path.insert(0, str(proj_root))
        runpy.run_path(tmp_path, run_name="__main__")
        serve_forever(timeout=args.timeout)
    except SystemExit:
        raise
    except BaseException:
        _print_traceback_with_source_map(tmp_path, smap, str(path))
        return 1
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass
    return 0


def cmd_deps(args: argparse.Namespace) -> int:
    """Manage runtime dependencies (libraries) declared in cobra4.toml."""
    import tomllib

    cfg_path = Path("cobra4.toml")

    def _load() -> dict:
        if not cfg_path.exists():
            return {}
        with open(cfg_path, "rb") as f:
            return tomllib.load(f)

    def _write_deps(deps: dict[str, str]) -> None:
        # Best-effort merge: preserve other sections by parsing then
        # rewriting only the `[deps]` table by string editing.
        existing = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else ""
        # Strip an existing [deps] section.
        out_lines, skip = [], False
        for line in existing.splitlines():
            if line.strip().startswith("[deps]"):
                skip = True
                continue
            if skip and line.startswith("["):
                skip = False
            if not skip:
                out_lines.append(line)
        body = "\n".join(out_lines).rstrip() + "\n\n[deps]\n"
        for name, ver in sorted(deps.items()):
            body += f'{name} = "{ver}"\n'
        cfg_path.write_text(body, encoding="utf-8")

    cfg = _load()
    deps: dict[str, str] = dict(cfg.get("deps", {}))

    if args.action == "list":
        if not deps:
            sys.stdout.write("(no dependencies declared)\n")
            return 0
        for name, ver in sorted(deps.items()):
            sys.stdout.write(f"{name} = {ver}\n")
        return 0
    if args.action == "add":
        if not args.name:
            sys.stderr.write("usage: c4 deps add NAME [--version VER]\n")
            return 2
        deps[args.name] = args.version or "*"
        _write_deps(deps)
        sys.stdout.write(f"added {args.name} = {deps[args.name]}\n")
        return 0
    if args.action == "remove":
        if not args.name or args.name not in deps:
            sys.stderr.write(f"no such dep '{args.name}'\n")
            return 2
        del deps[args.name]
        _write_deps(deps)
        sys.stdout.write(f"removed {args.name}\n")
        return 0
    if args.action == "install":
        if not deps:
            sys.stdout.write("(nothing to install)\n")
            return 0
        pkgs = []
        for name, ver in deps.items():
            pkgs.append(
                name
                if ver in ("*", "")
                else f"{name}{ver if ver[0] in '<>=!~' else '=='+ver}"
            )
        import subprocess as _sp

        py_exe = sys.executable
        if args.venv:
            venv_path = Path(args.venv if args.venv != "auto" else "./.cobra4/venv")
            if not venv_path.exists():
                sys.stdout.write(f"creating venv at {venv_path}\n")
                _sp.run([sys.executable, "-m", "venv", str(venv_path)], check=True)
            scripts = "Scripts" if os.name == "nt" else "bin"
            py_exe = str(
                venv_path / scripts / ("python.exe" if os.name == "nt" else "python")
            )
        cmd = [py_exe, "-m", "pip", "install", *pkgs]
        sys.stdout.write("$ " + " ".join(cmd) + "\n")
        return _sp.call(cmd)
    sys.stderr.write(f"unknown deps action '{args.action}'\n")
    return 64


def cmd_doc(args: argparse.Namespace) -> int:
    """Extract docstrings + signatures from a .c4 file → markdown or HTML."""
    path = Path(args.file)
    src = path.read_text(encoding="utf-8")
    try:
        pre = preprocess(src)
    except ValueError as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    try:
        module = parse(pre.source, source_path=str(path))
    except ParseError as e:
        sys.stderr.write(str(e) + "\n")
        return 2

    if args.html:
        out_path = Path(args.output) if args.output else path.with_suffix(".html")
        cmd_doc_html(module, out_path)
        sys.stdout.write(f"wrote {out_path}\n")
        return 0

    from cobra4 import ast_nodes as N

    out = [f"# {path.stem}\n"]
    for s in module.body:
        if isinstance(s, N.FnDecl):
            params = ", ".join(_param_doc(p) for p in s.params)
            ret = f" -> {_type_doc(s.return_type)}" if s.return_type else ""
            out.append(f"## `fn {s.name}({params}){ret}`\n")
            doc = _extract_docstring(s.block)
            if doc:
                out.append(doc + "\n")
        elif isinstance(s, N.ClassDecl):
            sup = (
                "(" + ", ".join(_type_doc(t) for t in s.supers) + ")"
                if s.supers
                else ""
            )
            out.append(f"## `class {s.name}{sup}`\n")
            doc = _extract_docstring(s.body)
            if doc:
                out.append(doc + "\n")
            for inner in s.body:
                if isinstance(inner, N.FnDecl):
                    params = ", ".join(_param_doc(p) for p in inner.params)
                    out.append(f"### `{inner.name}({params})`\n")
                    idoc = _extract_docstring(inner.block)
                    if idoc:
                        out.append(idoc + "\n")
    text = "\n".join(out)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def _param_doc(p) -> str:
    s = p.name
    if p.type_ref is not None:
        s += f": {_type_doc(p.type_ref)}"
    if p.default is not None:
        from cobra4.tools.fmt import _expr as _fmt_expr

        s += f" = {_fmt_expr(p.default)}"
    return s


def _type_doc(t) -> str:
    if t is None:
        return "Any"
    base = t.name + ("?" if t.optional else "")
    if t.args:
        return base + "[" + ", ".join(_type_doc(a) for a in t.args) + "]"
    return base


def _extract_docstring(body):
    """If the first statement is a string literal, return it as the doc."""
    from cobra4 import ast_nodes as N

    if not body:
        return None
    s = body[0]
    if isinstance(s, N.ExprStmt) and isinstance(s.value, N.Str):
        return s.value.value.strip()
    return None


def cmd_plugin(args: argparse.Namespace) -> int:
    """Manage language plugins.

    Subcommands:
      - ``list``: show registered plugins (built-in + installed third-party).
      - ``add NAME``: pip-install ``cobra4-lang-<NAME>`` from PyPI (or git URL).
      - ``remove NAME``: pip uninstall the plugin package.
    """
    if args.action == "list":
        import pkgutil
        import cobra4.plugins.builtin as builtin_pkg

        for mod in pkgutil.iter_modules(builtin_pkg.__path__):
            try:
                __import__(f"cobra4.plugins.builtin.{mod.name}")
            except ImportError:
                pass
        plugins = plugin_list()
        if not plugins:
            sys.stdout.write("(no plugins registered)\n")
            return 0
        for p in plugins:
            sys.stdout.write(f"{p.name}: {p.description}\n")
        return 0
    if args.action == "add":
        if not args.name:
            sys.stderr.write("usage: c4 plugin add NAME\n")
            return 2
        import subprocess as _sp

        target = (
            args.name
            if "://" in args.name or args.name.startswith("git+")
            else f"cobra4-lang-{args.name}"
        )
        cmd = [sys.executable, "-m", "pip", "install", target]
        sys.stdout.write("$ " + " ".join(cmd) + "\n")
        return _sp.call(cmd)
    if args.action == "remove":
        if not args.name:
            sys.stderr.write("usage: c4 plugin remove NAME\n")
            return 2
        import subprocess as _sp

        target = (
            args.name
            if args.name.startswith("cobra4-lang-")
            else f"cobra4-lang-{args.name}"
        )
        cmd = [sys.executable, "-m", "pip", "uninstall", "-y", target]
        sys.stdout.write("$ " + " ".join(cmd) + "\n")
        return _sp.call(cmd)
    sys.stderr.write(f"unknown plugin action '{args.action}'\n")
    return 64


def cmd_bench(args: argparse.Namespace) -> int:
    """Run the built-in micro-benchmarks. Useful for catching
    regressions before merging — save a baseline with
    ``c4 bench --json baseline.json``, then on each PR
    ``c4 bench --compare baseline.json``."""
    from cobra4.tools.bench import cli_main

    return cli_main(
        args.targets,
        seconds=args.seconds,
        json_path=args.json_out,
        compare_path=args.compare,
    )


def cmd_init(args: argparse.Namespace) -> int:
    """Scaffold a new project from a template."""
    from cobra4 import templates as _templates

    if args.list_templates:
        print("Available templates:")
        for name, fn in sorted(_templates.TEMPLATES.items()):
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            print(f"  {name:<14}  {doc}")
        return 0

    if not args.name:
        print(
            "error: missing project name. Usage: c4 init NAME [--template TYPE]",
            file=sys.stderr,
        )
        return 1

    target = Path(args.name)
    if target.exists() and not args.force:
        print(
            f"error: {target}/ already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    try:
        files = _templates.render(args.template, target.name)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for rel, content in files.items():
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)

    print(f"created {len(files)} files in {target}/")
    print()
    print("Next steps:")
    print(f"  cd {target}")
    if args.template == "etl-pipeline":
        print(f"  c4 run src/main.c4")
    elif args.template in ("http-service", "daemon"):
        print(f"  c4 run src/main.c4   # one-shot smoke test")
        print(f"  c4 serve src/main.c4 # boot the daemon")
    elif args.template == "agent":
        print(f"  c4 run src/main.c4   # mock provider, offline")
    return 0


def cmd_infra(args: argparse.Namespace) -> int:
    """Declarative infrastructure: import the file (collecting resource
    declarations as a side effect), then run the requested phase
    (``plan`` / ``apply`` / ``destroy``)."""
    from cobra4.runtime import infra as infra_mod

    infra_mod.clear_registry()
    path = Path(args.file)
    code, smap = _compile_file(path)
    with tempfile.NamedTemporaryFile(
        suffix=".py",
        prefix=f"{path.stem}__c4infra_",
        mode="w",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(code)
        tmp_path = tmp.name
    try:
        proj_root = Path(__file__).resolve().parent.parent
        if str(proj_root) not in sys.path:
            sys.path.insert(0, str(proj_root))
        runpy.run_path(tmp_path, run_name="__main__")
    except SystemExit:
        raise
    except BaseException:
        _print_traceback_with_source_map(tmp_path, smap, str(path))
        return 1
    finally:
        try:
            Path(tmp_path).unlink()
        except OSError:
            pass

    state_path = Path(args.state_file) if args.state_file else None
    if args.action == "plan":
        actions = infra_mod.plan(state_path)
        if not actions:
            print("(no resources declared)")
            return 0
        for name, action in actions:
            line = f"  {action.kind.upper():>7}  {name:<24}  {action.notes}"
            print(line)
            for k, (old, new) in action.diff.items():
                print(f"           {k}: {old!r} -> {new!r}")
        return 0
    if args.action == "apply":
        new_state = infra_mod.apply(state_path)
        print(f"applied {len(new_state)} resource(s):")
        for name, st in new_state.items():
            keys = ", ".join(f"{k}={v!r}" for k, v in st.items() if k != "contents")
            print(f"  {name:<24}  {keys}")
        return 0
    if args.action == "destroy":
        infra_mod.destroy(state_path)
        print("destroyed all resources from state")
        return 0
    print(f"unknown infra action: {args.action}", file=sys.stderr)
    return 2


def cmd_test(args: argparse.Namespace) -> int:
    """Discover + run cobra4 tests.

    Looks under each ``paths`` arg for ``test_*.c4``; defaults to
    ``tests`` and ``test``. Each top-level ``test_*`` function in those
    files is run as an isolated test case.
    """
    from cobra4.test_runner import run as run_tests

    summary = run_tests(args.paths, verbose=args.verbose, junit_xml=args.junit_xml)
    return 0 if summary.failed == 0 else 1


def cmd_run_watch(args: argparse.Namespace) -> int:
    """Run a .c4 file, then re-run on every change to it (or its imports)."""
    import time as _time

    path = Path(args.file).resolve()
    sources = [path]
    if args.watch_dir:
        for d in args.watch_dir:
            for p in Path(d).rglob("*.c4"):
                sources.append(p.resolve())

    last_mtimes: dict[Path, float] = {}

    def _mtimes() -> dict[Path, float]:
        out: dict[Path, float] = {}
        for s in sources:
            if s.exists():
                out[s] = s.stat().st_mtime
        return out

    sys.stdout.write(f"watching {len(sources)} file(s) — Ctrl-C to stop\n")
    last_mtimes = _mtimes()

    # First run
    cmd_run(argparse.Namespace(file=str(path)))

    try:
        while True:
            _time.sleep(0.5)
            cur = _mtimes()
            if cur != last_mtimes:
                sys.stdout.write("\n--- change detected, re-running ---\n")
                last_mtimes = cur
                # reset registries between runs for isolation
                from cobra4.runtime.core import reset_registries

                reset_registries()
                try:
                    cmd_run(argparse.Namespace(file=str(path)))
                except SystemExit:
                    pass
    except KeyboardInterrupt:
        sys.stdout.write("\nstopped\n")
        return 0


def cmd_doc_html(module, output_path: Path) -> None:
    """Render module → standalone HTML doc."""
    from cobra4 import ast_nodes as N

    parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'>",
        "<style>",
        "body{font-family:system-ui;max-width:48rem;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#222}",
        "h1{border-bottom:2px solid #0aa;padding-bottom:.5rem}",
        "h2{margin-top:2rem;color:#0aa}",
        "h3{color:#666;font-size:1.05rem}",
        "code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px;font-size:.9em}",
        "pre{background:#f4f4f4;padding:.8rem;border-radius:5px;overflow-x:auto}",
        ".doc{margin:.5rem 0 1.5rem;color:#444}",
        "</style></head><body>",
    ]
    for s in module.body:
        if isinstance(s, N.FnDecl):
            params = ", ".join(_param_doc(p) for p in s.params)
            ret = f" -&gt; {_type_doc(s.return_type)}" if s.return_type else ""
            parts.append(f"<h2><code>fn {s.name}({params}){ret}</code></h2>")
            doc = _extract_docstring(s.block)
            if doc:
                parts.append(f"<p class='doc'>{_html_escape(doc)}</p>")
        elif isinstance(s, N.ClassDecl):
            sup = (
                "(" + ", ".join(_type_doc(t) for t in s.supers) + ")"
                if s.supers
                else ""
            )
            parts.append(f"<h2><code>class {s.name}{sup}</code></h2>")
            doc = _extract_docstring(s.body)
            if doc:
                parts.append(f"<p class='doc'>{_html_escape(doc)}</p>")
            for inner in s.body:
                if isinstance(inner, N.FnDecl):
                    params = ", ".join(_param_doc(p) for p in inner.params)
                    parts.append(f"<h3><code>{inner.name}({params})</code></h3>")
                    idoc = _extract_docstring(inner.block)
                    if idoc:
                        parts.append(f"<p class='doc'>{_html_escape(idoc)}</p>")
    parts.append("</body></html>")
    output_path.write_text("\n".join(parts), encoding="utf-8")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cmd_unimplemented(name: str):
    def _inner(_args: argparse.Namespace) -> int:
        sys.stderr.write(
            f"`c4 {name}` is not implemented yet (planned for a later milestone).\n"
        )
        return 64

    return _inner


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    run_program_args: list[str] = []
    if argv and argv[0] == "run" and "--" in argv:
        sep = argv.index("--")
        run_program_args = argv[sep + 1 :]
        argv = argv[:sep]

    p = argparse.ArgumentParser(prog="c4", description="cobra4 CLI")
    p.add_argument("--version", action="version", version=f"cobra4 {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Transpile and execute a .c4 file")
    p_run.add_argument("file")
    p_run.add_argument("--watch", action="store_true", help="Re-run on file changes")
    p_run.add_argument("--watch-dir", action="append", help="Additional dir to watch")
    p_run.set_defaults(handler=lambda a: cmd_run_watch(a) if a.watch else cmd_run(a))

    p_test = sub.add_parser("test", help="Run cobra4 tests (test_*.c4)")
    p_test.add_argument("paths", nargs="*", default=["tests", "test"])
    p_test.add_argument("-v", "--verbose", action="store_true")
    p_test.add_argument("--junit-xml", help="Write JUnit XML report to PATH")
    p_test.set_defaults(handler=cmd_test)

    p_build = sub.add_parser("build", help="Transpile to Python without running")
    p_build.add_argument("file")
    p_build.add_argument(
        "-o", "--output", default="-", help="Output path or '-' for stdout"
    )
    p_build.add_argument(
        "--source-map", action="store_true", help="Write .pymap alongside output"
    )
    p_build.set_defaults(handler=cmd_build)

    p_fmt = sub.add_parser("fmt", help="Format / validate a .c4 file")
    p_fmt.add_argument("file")
    p_fmt.add_argument("-w", "--write", action="store_true", help="Write back in place")
    p_fmt.set_defaults(handler=cmd_fmt)

    p_check = sub.add_parser("check", help="Lint / validate a .c4 file (no execution)")
    p_check.add_argument("file")
    p_check.add_argument(
        "--strict", action="store_true", help="Treat warnings as errors"
    )
    p_check.add_argument(
        "--no-undefined", action="store_true", help="Disable undefined-name warnings"
    )
    p_check.add_argument(
        "--no-shadowing", action="store_true", help="Disable shadowing warnings"
    )
    p_check.add_argument(
        "--no-types", action="store_true", help="Disable gradual type checking"
    )
    p_check.add_argument(
        "--no-dispatch", action="store_true", help="Disable dispatcher overlap analysis"
    )
    p_check.set_defaults(handler=cmd_check)

    p_repl = sub.add_parser("repl", help="Interactive REPL")
    p_repl.set_defaults(handler=cmd_repl)

    p_idle = sub.add_parser("idle", help="Open the cobra4 browser IDLE")
    p_idle.add_argument("--host", default="127.0.0.1")
    p_idle.add_argument("--port", type=int, default=8765)
    p_idle.add_argument("--no-browser", action="store_true")
    p_idle.add_argument("--verbose", action="store_true")
    p_idle.set_defaults(handler=cmd_idle)

    p_lsp = sub.add_parser("lsp", help="Run the cobra4 language server on stdio")
    p_lsp.set_defaults(
        handler=lambda _a: __import__("cobra4.tools.lsp", fromlist=["run"]).run()
    )

    p_serve = sub.add_parser(
        "serve", help="Run .c4 file as a daemon (every/on event/serve)"
    )
    p_serve.add_argument("file")
    p_serve.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Stop after N seconds (used for tests; default = run until Ctrl-C)",
    )
    p_serve.set_defaults(handler=cmd_serve)

    p_bench = sub.add_parser("bench", help="Run cobra4's built-in benchmarks")
    p_bench.add_argument(
        "targets",
        nargs="*",
        help="Subset to run (parser, codegen, smart-dispatch, workflow, async-parallel). Empty = all.",
    )
    p_bench.add_argument(
        "--seconds",
        type=float,
        default=1.0,
        help="Wall-time budget per target (default 1.0)",
    )
    p_bench.add_argument(
        "--json", dest="json_out", help="Write per-target results to a JSON file"
    )
    p_bench.add_argument(
        "--compare", dest="compare", help="Compare against a previously-saved JSON file"
    )
    p_bench.set_defaults(handler=cmd_bench)

    p_init = sub.add_parser(
        "init", help="Scaffold a new cobra4 project from a template"
    )
    p_init.add_argument(
        "name", nargs="?", help="Project directory name (must not exist)"
    )
    p_init.add_argument(
        "--template",
        "-t",
        default="http-service",
        help="Template: http-service | etl-pipeline | agent | daemon (default: http-service)",
    )
    p_init.add_argument(
        "--list",
        action="store_true",
        dest="list_templates",
        help="List available templates and exit",
    )
    p_init.add_argument(
        "--force", action="store_true", help="Allow writing into an existing directory"
    )
    p_init.set_defaults(handler=cmd_init)

    p_infra = sub.add_parser(
        "infra", help="Declarative infrastructure (resource declarations)"
    )
    p_infra.add_argument("action", choices=["plan", "apply", "destroy"])
    p_infra.add_argument("file", help="cobra4 file containing `resource` blocks")
    p_infra.add_argument(
        "--state-file",
        default=None,
        help="Override state-file path (default: ./.cobra4/state.json)",
    )
    p_infra.set_defaults(handler=cmd_infra)

    p_plugin = sub.add_parser("plugin", help="Manage language plugins")
    p_plugin.add_argument(
        "action", choices=["list", "add", "remove"], default="list", nargs="?"
    )
    p_plugin.add_argument("name", nargs="?", help="Plugin name (for add/remove)")
    p_plugin.set_defaults(handler=cmd_plugin)

    p_deps = sub.add_parser(
        "deps", help="Manage runtime dependencies (cobra4.toml [deps])"
    )
    p_deps.add_argument("action", choices=["list", "add", "remove", "install"])
    p_deps.add_argument("name", nargs="?", help="Dependency name (for add/remove)")
    p_deps.add_argument("--version", help="Pinned version (for add)")
    p_deps.add_argument(
        "--venv",
        nargs="?",
        const="auto",
        help="Install into a project-local venv (default: ./.cobra4/venv). "
        "Pass an explicit path to override.",
    )
    p_deps.set_defaults(handler=cmd_deps)

    p_doc = sub.add_parser("doc", help="Extract docstrings + signatures to markdown")
    p_doc.add_argument("file")
    p_doc.add_argument("-o", "--output", help="Output file (default: stdout)")
    p_doc.add_argument(
        "--html", action="store_true", help="Output a standalone HTML page"
    )
    p_doc.set_defaults(handler=cmd_doc)

    args = p.parse_args(argv)
    if getattr(args, "cmd", None) == "run":
        args.program_args = run_program_args
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
