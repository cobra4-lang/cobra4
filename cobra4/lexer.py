"""Lexer for cobra4.

Wraps lark's tokenizer plus a postlex hook that:

- suppresses ``_NL`` tokens while inside ``(`` or ``[`` (so multi-line
  argument lists and list literals don't terminate statements);
- leaves ``{ }`` alone — the grammar disambiguates block vs dict literal
  by context.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Optional

from lark import Lark
from lark.lexer import Token
from lark.indenter import PostLex


GRAMMAR_PATH = Path(__file__).with_name("grammar.lark")


class _BracketAwarePostLex(PostLex):
    """Drop ``_NL`` tokens inside ``()``, ``[]``, or a dict-literal ``{}``.

    Distinguishing a dict-literal ``{`` from a block ``{`` uses a
    "expecting block" flag set whenever the lexer sees a control-flow
    or declaration keyword that REQUIRES a block to follow
    (``if``/``while``/``for``/``each``/``every``/``fn``/``class``/
    ``match``/``case``/``try``/``catch``/``finally``/``elif``/``else``/
    ``deploy``/``on``/``do``).

    The flag stays set until we see a ``{`` (treated as a block) or
    until we see a token that makes "block expected here" implausible
    (e.g. ``;``, certain expression-only operators).
    """

    always_accept = ("_NL",)

    # Lark turns string-literal keywords in the grammar into
    # uppercase-named terminals (e.g. `"if"` → terminal `IF`). We match
    # on token *type*, not value, because keywords are NOT classified as
    # `NAME` once Lark has tokenized them.
    _BLOCK_KEYWORD_TYPES = frozenset({
        "IF", "ELIF", "ELSE", "WHILE", "FOR", "EACH", "EVERY", "ON",
        "FN", "CLASS", "MATCH", "CASE", "TRY", "CATCH", "FINALLY",
        "DEPLOY", "DO",
    })
    _BLOCK_KEYWORD_VALUES = frozenset({
        "if", "elif", "else", "while", "for", "each", "every", "on",
        "fn", "class", "match", "case", "try", "catch", "finally",
        "deploy", "do",
    })

    # Keywords that ALWAYS introduce a block (no ambiguity with ternary
    # / expression-level uses).
    _UNAMBIGUOUS_BLOCK_TYPES = frozenset({
        "IF", "WHILE", "FOR", "EACH", "EVERY", "ON", "FN", "CLASS",
        "MATCH", "CASE", "TRY", "CATCH", "FINALLY", "DEPLOY", "DO",
    })
    _UNAMBIGUOUS_BLOCK_VALUES = frozenset({
        "if", "while", "for", "each", "every", "on", "fn", "class",
        "match", "case", "try", "catch", "finally", "deploy", "do",
    })
    # `else` and `elif` are block-only when they FOLLOW a `}` (closing
    # the prior arm). In ternary `... if cond else expr` they appear
    # right after an expression with no `}` in between.
    _CONTEXTUAL_BLOCK_TYPES = frozenset({"ELSE", "ELIF"})
    _CONTEXTUAL_BLOCK_VALUES = frozenset({"else", "elif"})

    def process(self, stream: Iterator[Token]) -> Iterator[Token]:
        depth_pl = 0
        depth_dict = 0
        brace_stack: list[bool] = []  # True = dict literal, False = block
        expecting_block = False
        last_seen: Optional[Token] = None  # previous non-_NL token

        for tok in stream:
            if tok.type == "LPAR" or tok.type == "LSQB":
                depth_pl += 1
            elif tok.type == "RPAR" or tok.type == "RSQB":
                if depth_pl > 0:
                    depth_pl -= 1
            elif tok.type == "LBRACE":
                is_dict = not (expecting_block and depth_pl == 0)
                brace_stack.append(is_dict)
                if is_dict:
                    depth_dict += 1
                expecting_block = False
            elif tok.type == "RBRACE":
                if brace_stack:
                    was_dict = brace_stack.pop()
                    if was_dict and depth_dict > 0:
                        depth_dict -= 1
            elif depth_pl == 0:
                # Unambiguous block-introducing keywords.
                if (
                    tok.type in self._UNAMBIGUOUS_BLOCK_TYPES
                    or (tok.type == "NAME" and tok.value in self._UNAMBIGUOUS_BLOCK_VALUES)
                ):
                    expecting_block = True
                # `else`/`elif` only count as block-introducing when they
                # immediately follow a closing `}` (an if-block arm).
                elif (
                    tok.type in self._CONTEXTUAL_BLOCK_TYPES
                    or (tok.type == "NAME" and tok.value in self._CONTEXTUAL_BLOCK_VALUES)
                ):
                    if last_seen is not None and last_seen.type == "RBRACE":
                        expecting_block = True

            # A toplevel newline ends a statement, so any "block expected"
            # context that wasn't satisfied by a `{` on the same line was
            # really an expression-level use (e.g. ternary `... else ...`).
            if (
                tok.type == "_NL"
                and depth_pl == 0
                and depth_dict == 0
            ):
                expecting_block = False

            if tok.type == "_NL" and (depth_pl > 0 or depth_dict > 0):
                continue
            yield tok
            if tok.type != "_NL":
                last_seen = tok


def make_parser() -> Lark:
    """Construct (and cache-on-import) the cobra4 LALR parser."""
    return Lark(
        GRAMMAR_PATH.read_text(encoding="utf-8"),
        parser="lalr",
        postlex=_BracketAwarePostLex(),
        propagate_positions=True,
        maybe_placeholders=False,
    )


_parser: Lark | None = None


def get_parser() -> Lark:
    """Return the singleton parser, building it on first use."""
    global _parser
    if _parser is None:
        _parser = make_parser()
    return _parser


def tokenize(source: str) -> list[Token]:
    """Return the post-processed token stream for ``source``.

    Useful for testing the lexer in isolation.
    """
    parser = get_parser()
    raw = parser.lex(source)
    return list(raw)
