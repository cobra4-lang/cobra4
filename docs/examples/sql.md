# SQL language plugin

`lang use sql` rewrites `sql { SELECT ... }` blocks into `sql_run("...")` calls. With `_sql.configure(url)` (or `COBRA4_SQL_URL`) execution is real via SQLAlchemy.

Source: [`examples/07_sql_plugin.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/07_sql_plugin.c4)

```cobra4
--8<-- "examples/07_sql_plugin.c4"
```

## Run it

```bash
c4 run examples/07_sql_plugin.c4
```
