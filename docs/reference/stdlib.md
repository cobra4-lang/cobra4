# Stdlib API

The cobra4 stdlib is **written in cobra4 itself** under
[`cobra4/stdlib/*.c4`](https://github.com/cobra4-lang/cobra4/tree/main/cobra4/stdlib)
and loaded via an mtime-cached import hook.

```cobra4
use cobra4.stdlib.http as http
use cobra4.stdlib.fs   as fs
```

## Modules at a glance

| Module                 | Highlights                                                          |
|------------------------|---------------------------------------------------------------------|
| `cobra4.stdlib.http`   | `Session`, `get`, `post`, retry policy, basic auth                  |
| `cobra4.stdlib.json`   | `parse`, `dump` (thin wrappers with sane defaults)                  |
| `cobra4.stdlib.fs`     | `walk`, `copy`, `copytree`, `move`, `read_text`                     |
| `cobra4.stdlib.data`   | `group_by`, `aggregate`, `join`, `sort_by`                          |
| `cobra4.stdlib.time`   | parse / format durations and timestamps                             |
| `cobra4.stdlib.strings`| `slugify`, `camel_to_snake`, `snake_to_camel`                       |
| `cobra4.stdlib.cli`    | `App` builder for argparse-style command trees                      |
| `cobra4.stdlib.test`   | assertion DSL used by `c4 test`                                     |

The full source per module is short and idiomatic — it's the easiest
way to learn cobra4 conventions.

## Caching

First import transpiles `<mod>.c4` to Python and writes
`__pycache__/<mod>.cobra4.pyc` keyed on source mtime + size. Edits
trigger a single re-transpile; otherwise it's a single file read.
