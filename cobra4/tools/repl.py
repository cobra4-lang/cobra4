"""Multi-line REPL for cobra4.

Continues reading input lines until the buffer parses cleanly. Detects
incomplete input via the parser's error message (``unexpected EOF``,
unclosed brackets, etc.) and switches the prompt to ``...``.

Persists locals between iterations so users can build up state.
"""

from __future__ import annotations

import sys
import textwrap
import traceback
from pathlib import Path
from typing import Optional

from cobra4 import __version__
from cobra4.parser import parse, ParseError
from cobra4.lowering import lower
from cobra4.codegen import generate


_RUNTIME_BOOT = textwrap.dedent("""
    from cobra4.runtime import (
        safe_attr as _c4_safe_attr,
        default as _c4_default,
        every as _c4_every,
        on_event as _c4_on_event,
        parallel_for as _c4_parallel_for,
        read, save, log, smart,
        Host, inventory, run, fan_out,
        secret,
        deploy as _c4_deploy,
        env_from,
        aws, gcp, azure, k8s, fly,
        queue, serve_forever,
    )
    from cobra4.runtime.core import serve_handler as _c4_serve
""")


def _is_incomplete(err: ParseError, buf: str) -> bool:
    """Heuristic: input is incomplete if brackets are still unbalanced.

    We count `(`, `[`, `{` minus their closers — naively, ignoring strings
    and comments since the lexer would already have rejected those.
    """
    depth = 0
    in_string = None  # current quote char or None
    i = 0
    n = len(buf)
    while i < n:
        c = buf[i]
        if in_string:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == in_string:
                in_string = None
            i += 1
            continue
        if c in ("\"", "'"):
            in_string = c
            i += 1
            continue
        if c == "#":
            while i < n and buf[i] != "\n":
                i += 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        i += 1
    return depth > 0


def run_repl(stream_in=None, stream_out=None) -> int:
    out = stream_out or sys.stdout
    inp = stream_in or sys.stdin
    out.write(f"cobra4 {__version__} REPL — Ctrl-D / Ctrl-Z to exit\n")

    proj_root = Path(__file__).resolve().parent.parent.parent
    if str(proj_root) not in sys.path:
        sys.path.insert(0, str(proj_root))
    locals_: dict = {}
    exec(_RUNTIME_BOOT, locals_)

    # History + completion via readline. Best-effort — Windows ships
    # `pyreadline3` for the same API; if neither is available we fall
    # back to a plain input loop.
    history_file = Path.home() / ".cobra4" / "history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        import readline  # noqa: F401  (real or pyreadline3)
        try:
            readline.read_history_file(str(history_file))
        except (FileNotFoundError, OSError):
            pass
        readline.set_history_length(2000)

        def _completer(text: str, state: int) -> Optional[str]:
            from cobra4.resolver import _PY_BUILTINS, _C4_BUILTINS

            keywords = [
                "if ", "elif ", "else ", "while ", "for ", "each ", "in ", "and ",
                "or ", "not ", "True", "False", "None", "fn ", "class ", "data ",
                "return ", "raise ", "break", "continue", "pass", "match ", "case ",
                "try ", "catch ", "finally ", "use ", "as ", "where ", "every ",
                "on ", "from ", "to ", "with ", "parallel", "serve ", "deploy ", "lang ",
            ]
            candidates = list(_PY_BUILTINS) + list(_C4_BUILTINS) + keywords + list(locals_.keys())
            matches = [c for c in candidates if c.startswith(text)]
            try:
                return matches[state]
            except IndexError:
                return None

        readline.set_completer(_completer)
        readline.parse_and_bind("tab: complete")
    except ImportError:
        readline = None  # type: ignore[assignment]

    buf = ""
    while True:
        prompt = "c4> " if not buf else "... "
        try:
            if inp is sys.stdin:
                line = input(prompt)
            else:
                out.write(prompt)
                out.flush()
                line = inp.readline()
                if not line:
                    raise EOFError
                line = line.rstrip("\n")
        except (EOFError, KeyboardInterrupt):
            out.write("\n")
            return 0

        if not line and not buf.strip():
            continue
        buf = buf + line + "\n" if buf else line + "\n"

        try:
            module = parse(buf, source_path="<repl>")
        except ParseError as e:
            if _is_incomplete(e, buf):
                continue
            out.write(str(e) + "\n")
            buf = ""
            continue

        # Save accepted input to history (atomically).
        try:
            if readline is not None:
                readline.write_history_file(str(history_file))
        except (OSError, NameError):
            pass

        try:
            module = lower(module)
            code = generate(module, cobra4_path="<repl>").code
            try:
                exec(code, locals_)
            except SystemExit:
                raise
            except BaseException:
                out.write(traceback.format_exc())
        except Exception as e:  # noqa: BLE001
            out.write(f"runtime error: {e}\n")
        buf = ""
