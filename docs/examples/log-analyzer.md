# Log analyzer

Parses an access log, groups entries by status code and path, computes per-bucket counts, and dumps a structured JSON report. Real-world data-pipeline shape in ~80 lines.

Source: [`examples/09_log_analyzer.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/09_log_analyzer.c4)

```cobra4
--8<-- "examples/09_log_analyzer.c4"
```

## Run it

```bash
c4 run examples/09_log_analyzer.c4
```
