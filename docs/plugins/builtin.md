# Built-in language plugins

## `sql`

```cobra4
lang use sql
use cobra4.plugins.builtin.sql as _sql

_sql.configure("sqlite:///./app.db")    # or set COBRA4_SQL_URL

sql_run("CREATE TABLE IF NOT EXISTS users (id TEXT, age INT)")

adults = sql {
    SELECT id, name FROM users WHERE age >= :min
}    # → list[dict]
```

The plugin rewrites `sql { ... }` blocks to `sql_run("...")` calls. With
`configure(url)` (or `COBRA4_SQL_URL`), execution is **real** via
SQLAlchemy. Without configuration, `sql_run` returns the raw SQL text
unchanged — useful for preview / dry-run.

## `regex`

```cobra4
lang use regex

p = re"\d{3}-\d{4}"i               # `i` flag → re.IGNORECASE
matches = each line in lines where p.search(line) { line }
```

`re"..."flags` compiles to `re.compile(..., flags)` at module load.

## `yaml`

```cobra4
lang use yaml

config = yaml"""
listen: 0.0.0.0:8080
debug: true
"""
log("config", port=config.listen)
```

Inline YAML is parsed at load time. Requires `pip install cobra4[yaml]`.
