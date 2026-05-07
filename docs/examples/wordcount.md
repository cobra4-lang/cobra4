# Word count

Reads the project's `README.md` as text, tokenizes by whitespace, counts case-folded occurrences, sorts by frequency, and saves the top-10 to `out_wordcount.json`. Demonstrates: smart `read` of a local text file, dict accumulation, `sorted(...)` with a `fn(kv) = kv[1]` key, slicing, and `save()` to JSON.

Source: [`examples/01_wordcount.c4`](https://github.com/cobra4-lang/cobra4/blob/main/examples/01_wordcount.c4)

```cobra4
--8<-- "examples/01_wordcount.c4"
```

## Run it

```bash
c4 run examples/01_wordcount.c4
```
