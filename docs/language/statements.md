# Statements

This page is being expanded. For now, the most complete reference is
[`AI_HELPER.md` § 3](https://github.com/cobra4-lang/cobra4/blob/main/AI_HELPER.md#3-statement)
in the repository.

## Quick tour

```cobra4
# if / elif / else
if x > 0 { log("pos") }
elif x == 0 { log("zero") }
else { log("neg") }

# while
while not done { tick() }

# for — side effects
for x in xs { process(x) }

# each — produces a list
results = each x in xs { x * 2 }

# each with filter
adults = each u in users where u.age >= 18 { u }

# each in parallel
fetched = each url in urls in parallel(workers=20) { fetch(url) }

# every — register a periodic callback (run with `c4 serve`)
every 5 minutes { rotate_logs() }

# on event — register a queue consumer
on event from queue("orders") { process(event) }

# match
match status {
    case 200 { handle(body) }
    case 404 | 410 { gone() }
    case s if s >= 500 { retry() }
    case _ { log.warn("unexpected", code=status) }
}

# try / catch / finally
try {
    risky()
} catch ValueError as e {
    log.error("bad input", err=str(e))
} finally {
    cleanup()
}
```

See [`grammar.lark`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/grammar.lark)
for the precise grammar.
