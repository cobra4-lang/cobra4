# AI assistants guide

The repository ships
[`AI_HELPER.md`](https://github.com/cobra4-lang/cobra4/blob/main/AI_HELPER.md) —
a structured spec written specifically for LLM consumption. No prose,
no narrative, just rules and patterns.

If you let an assistant write cobra4 code, point it at that file (or
embed it in your system prompt). It enumerates:

- exactly which keywords / operators / literals exist (and which **don't**),
- common DON'Ts (`def` instead of `fn`, list comprehension instead of
  `each`, type-annotated assignments, …),
- canonical patterns for the cloud primitives,
- a checklist the assistant can run against before generating a `.c4`.

The same content is loaded into the project's auto memory if you use
Claude Code in this repo — see [`CLAUDE.local.md`](https://github.com/cobra4-lang/cobra4)
(gitignored) for any project-specific overrides.
