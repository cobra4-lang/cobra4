# Pattern matching

cobra4's `match` covers literal, name-binding, list, tuple, dict, and
constructor patterns, with **OR-patterns** and **guards**.

```cobra4
match value {
    case None             { return }
    case 200              { ok() }
    case 404 | 410 | 451  { gone() }                     # OR-pattern
    case [first, *rest]   { handle(first, rest) }        # list rest
    case [a, b, c]        { handle3(a, b, c) }
    case (method, path)   { route(method, path) }        # tuple destructure
    case Point(x, y)      { use_xy(x, y) }               # constructor
    case {"key": v, **other} { use(v, other) }           # dict + rest
    case s if s.startswith("http") { handle_url(s) }     # guard
    case _                { log.warn("unmatched") }
}
```

Range patterns (`case 1..10`) are **not** supported — write the guard
explicitly: `case x if 1 <= x <= 10 { ... }`.
