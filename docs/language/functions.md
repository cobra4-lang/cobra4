# Functions & classes

## Functions

```cobra4
# inline (single-expression body)
fn double(x) = x * 2
fn greet(name: str) -> str = "hello {name}"

# block body
fn sum(xs: list) -> int {
    "Docstring (first string is treated as doc by `c4 doc`)."
    total = 0
    for x in xs { total += x }
    return total
}

# defaults
fn greet(name="world") = "hello {name}"

# *args and **kwargs
fn build(**opts) { log("opts", **opts) }

# anonymous lambdas
double = fn(x) = x * 2
add    = fn(a, b) { return a + b }
```

## Decorators

```cobra4
@smart
fn process(target) { return target }

@cache(ttl=60)
fn fetch_user(id) { ... }
```

Decorators are stacked one per line, before the signature. `@smart`
turns a function into a [SmartFn](smart-dispatch.md).

## Classes

```cobra4
class User {
    fn __init__(self, name, age) {
        self.name = name
        self.age  = age
    }
    fn is_adult(self) -> bool = self.age >= 18
    fn __repr__(self) = "User({self.name}, {self.age})"
}

class Admin(User) {
    fn __init__(self, name, age, perms) {
        super().__init__(name, age)
        self.perms = perms
    }
}
```

There is **no** shorthand `data class Point(x, y)` in cobra4. For a
dataclass-style declaration, use Python's `@dataclasses.dataclass`:

```cobra4
use dataclasses

@dataclasses.dataclass
class Point {
    x: int = 0
    y: int = 0
}
```
