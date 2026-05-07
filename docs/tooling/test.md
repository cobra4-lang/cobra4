# Test framework

`c4 test` discovers files matching `tests/test_*.c4` and runs them with
a pytest-style reporter.

```cobra4
# tests/test_smoke.c4
use cobra4.stdlib.test as t

fn test_addition() {
    t.assert_eq(1 + 1, 2)
}

fn test_membership() {
    t.assert_in("a", ["a", "b", "c"])
}
```

```bash
c4 test                                 # full discovery
c4 test tests/test_smoke.c4             # single file
c4 test --verbose
c4 test --junit-xml=junit.xml           # CI-friendly output
```

The assertion DSL lives in
[`cobra4/stdlib/test.c4`](https://github.com/cobra4-lang/cobra4/blob/main/cobra4/stdlib/test.c4):

| Helper                              | What it asserts                        |
|-------------------------------------|----------------------------------------|
| `assert_eq(a, b)`                   | `a == b`                               |
| `assert_ne(a, b)`                   | `a != b`                               |
| `assert_in(x, xs)`                  | `x in xs`                              |
| `assert_close(a, b, tol=1e-6)`      | `abs(a - b) <= tol`                    |
| `assert_raises(exc, fn)`            | calling `fn()` raises `exc`            |

Failures show source-mapped tracebacks (`line:col` of the cobra4 file,
not the generated Python).
