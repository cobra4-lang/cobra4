---
hide:
  - navigation
---

<div align="center" markdown>
<img src="assets/logo-128.png" alt="cobra4 logo" width="96"/>

# cobra4

**A high-level, cloud-native language transpiled to Python.**

</div>

cobra4 promotes patterns common in cloud automation, data pipelines, and
distributed jobs to first-class language constructs. *One line of cobra4
often replaces a small Python program.*

[:material-rocket-launch: Get started](getting-started/install.md){ .md-button .md-button--primary }
[Cobra4 Studio](tooling/idle.md){ .md-button }
[:material-github: GitHub](https://github.com/cobra4-lang/cobra4){ .md-button }

---

## Three lines that show what cobra4 *is*

### 1. ETL across formats — `read`/`save` are smart-dispatched

```cobra4
rows   = read("./users.csv")
adults = each r in rows where int(r["age"]) >= 18 { r }
save(adults, "./adults.json")
```

### 2. Webhook server, with auth and pattern-matched routing

```cobra4
fn handler(req) {
    if req?.headers?.authorization != "Bearer secret" {
        return (401, {}, {"error": "nope"})
    }
    match (req.method, req.path) {
        case ("GET",  "/health") { return {"ok": true} }
        case ("POST", "/users")  { return create_user(req.json()) }
        case _                   { return (404, {}, {}) }
    }
}

serve handler on :8080
```

### 3. Scheduled job + parallel fan-out

```cobra4
urls = read("./targets.txt")

every 5 minutes {
    results = each url in urls in parallel(workers=10) { fetch(url) }
    save(results, "s3://bucket/snapshots/{now()}.jsonl")
}
```

---

## Cobra4 Studio

Run `c4 studio` inside a Cobra4 project to open the browser IDE. Studio
combines the project tree, file actions, project search, syntax
highlighting, completions, lint diagnostics, snippets, a terminal,
generated Python with source-map highlighting, and a graph of the
program's runtime intent.

---

## Mantra

1. **Readability first** — no esoteric operators (no `|>`), English keywords.
2. **One line = one program** — cloud / distributed patterns are syntax.
3. **General-purpose** — anything Python does, cobra4 does.
4. **Extensible on two axes** — *libraries* extend the runtime,
   *language plugins* (`lang use sql`) extend the parser/AST.
