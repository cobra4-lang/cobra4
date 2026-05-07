# Examples

Every example here lives at `examples/*.c4` in the repository, runs
end-to-end with `c4 run`, and is exercised by the test suite — they're
not pseudo-code.

| # | Example                                        | What it shows                                       |
|---|------------------------------------------------|-----------------------------------------------------|
| 01 | [Word count](wordcount.md)                    | `read` text, dict counter, `sort_by`, `save` JSON   |
| 02 | [Health check](healthcheck.md)                | `each ... in parallel`, http GET, aggregation       |
| 03 | [ETL](etl.md)                                 | format roundtrip via smart dispatch (CSV → JSON)    |
| 04 | [HTTP serve](serve.md)                        | `serve handler on :8080`, request shape             |
| 05 | [Schedule](schedule.md)                       | `every N seconds`, scheduler driving                |
| 06 | [Fleet](fleet.md)                             | inventory + parallel `run` over hosts               |
| 07 | [SQL plugin](sql.md)                          | `lang use sql` + real SQLAlchemy execution          |
| 08 | [Stdlib dogfood](stdlib.md)                   | importing the cobra4-written stdlib                 |
| 09 | [Log analyzer](log-analyzer.md)               | parse logs → per-status / per-path aggregates       |
| 10 | [Webhook router](webhook-router.md)           | bearer auth + match-routed handler + sql plugin     |

## Running them

```bash
git clone https://github.com/cobra4-lang/cobra4.git
cd cobra4
pip install -e .
c4 run examples/03_etl.c4
```
