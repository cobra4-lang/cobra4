# Authoring a language plugin

A **language plugin** extends the parser/AST. It is **not** a runtime
library — see `LanguagePlugin` in
[`cobra4/plugins/api.py`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/plugins/api.py).

## Anatomy

```python
# cobra4_lang_kafka/__init__.py
from cobra4.plugins.api import LanguagePlugin

def transform_source(src: str) -> str:
    """Rewrite kafka { ... } blocks into kafka_send("...") calls."""
    ...

PLUGIN = LanguagePlugin(
    name="kafka",
    transform_source=transform_source,
    runtime_module="cobra4_lang_kafka.runtime",  # imported on activation
    builtins=["kafka_send"],                      # known to resolver/checker
    description="Kafka literal blocks.",
)
```

## Activation

The user opts in by writing `lang use NAME` at the **top** of a `.c4`
file:

```cobra4
lang use kafka

kafka {
    topic = "orders"
    payload = {"id": 42}
}
```

The cobra4 loader scans for `lang use` directives, imports
`cobra4.plugins.builtin.<name>` if present, otherwise tries
`cobra4_lang_<name>` from `pip`-installed packages.

## Format-preserving

Set `preserve_for_format=True` on the plugin so `c4 fmt` keeps the
plugin block verbatim instead of trying to canonicalize it.

## Reference plugins

- [`sql`](builtin.md) — rewrites `sql { SELECT ... }` to `sql_run("...")`.
- [`regex`](builtin.md) — adds `re"pattern"flags` literals.
- [`yaml`](builtin.md) — adds `yaml"""..."""` inline literals.
