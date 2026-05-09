# AI_HELPER.md — guida operativa a cobra4 per assistenti AI

Questo file è la fonte autoritativa quando un'AI deve scrivere, leggere o
modificare codice cobra4. È scritto per essere consumato direttamente —
non aspettarti narrativa, aspettati specifiche e regole.

> **Leggimi prima di toccare un `.c4`**. Se una sezione dice "non
> supportato", non aggirarla con tentativi: usa la forma indicata.

---

## 0. Mental model

cobra4 è un linguaggio **transpilato a Python** (stesse semantiche di
runtime: oggetti mutabili, `None`, exception, type di Python). La
sintassi è propria, **brace-based**, con keyword inglesi e zero operatori
esoterici. La filosofia centrale:

1. **Leggibilità prima di tutto.** Niente `|>`, niente UFCS, niente macro.
2. **Una riga = un programma Python.** Cloud/distributed pattern come
   keyword native (`each ... in parallel`, `every Ns { ... }`,
   `serve handler on :port`, `deploy x to target`).
3. **Smart dispatch.** `read`/`save`/`run` decidono cosa fare in base a
   tipo, scheme URI, estensione, MIME. Aperto: librerie possono registrarsi.
4. **Plugin di linguaggio ≠ librerie.** I primi estendono parser/AST
   (`lang use sql`); le seconde estendono runtime (`use http`).

Quando in dubbio: **scrivi cobra4 come scriveresti Python idiomatico, ma
con `{ }` e tieni gli operatori standard tranne `?.` e `??`.**

---

## 1. Lessico

### 1.1 Identificatori e keyword

Identifier regex: `[A-Za-z_][A-Za-z0-9_]*`.

Keyword riservate (non usabili come nomi di variabile):

```
if elif else while for each in not and or is True False None
fn class data return raise break continue pass
match case try catch finally
use as where every on event from to parallel with
serve deploy lang
async await
workflow task resource
```

### 1.2 Commenti

```cobra4
# stile Python — l'unico supportato
```

**Solo `#`** apre un commento. `//` è **sempre** floor division — versioni
preliminari accettavano `//` come commento C-style ma c'era un conflitto
ambiguo con l'operatore di divisione intera.

Niente commenti multi-line: usa più righe singole.

### 1.3 Letterali numerici

```cobra4
n = 42                # int
n = 3.14              # float
n = 1.5e-3            # float scientifico
```

**Non supportato in M5**: hex/oct/bin literals (`0x1F`, `0b101`),
underscore in numeri (`1_000_000`). Usa `int("0x1F", 16)` se necessario.

### 1.4 Stringhe

```cobra4
s = "hello"                    # string standard
s = 'hello'                    # equivalente
s = "hello {name}"             # interpolazione di default (no prefix `f`)
s = "literal {{braces}}"       # `{{` e `}}` escape per parentesi letterali
s = "line1\nline2"             # escape standard
s = r"C:\Users\path"           # raw string (no interpretazione escape)
s = """
multi
line
"""                            # triple quote
```

**Tutte** le stringhe non-raw sono interpolate. Niente prefix `f`. Non c'è
formato esplicito tipo `"{x:.2f}"` — usa `format(x, ".2f")` o
`"{round(x, 2)}"`.

### 1.5 Booleani / None

`True`, `False`, `None` (capitalizzati come Python).

### 1.6 Operatori

| Categoria | Operatori |
|---|---|
| Aritmetica | `+`, `-`, `*`, `/`, `//`, `%`, `**` |
| Bitwise | `&`, `\|`, `^`, `~`, `<<`, `>>` |
| Comparison | `==`, `!=`, `<`, `>`, `<=`, `>=`, `is`, `is not`, `in`, `not in` |
| Bool | `and`, `or`, `not` |
| Special | `?.` (safe-nav), `??` (null-default) |
| Assign | `=`, `+=`, `-=`, `*=`, `/=`, `//=`, `%=`, `**=`, `&=`, `\|=`, `^=`, `<<=`, `>>=` |

**Non esistono**: `|>`, walrus `:=`, ternario `?:` (usa `x if c else y`),
`@` come matrix multiply.

### 1.7 Safe-nav `?.` e default `??`

Sono i due unici operatori "speciali" di cobra4. Servono questo pattern:

```cobra4
# Contro `getattr(obj, "attr", None)` e dict access che rompe.
name = req?.params?.name ?? "anon"

# Funziona con:
#   req=None              → name="anon"
#   req={params: None}    → name="anon"
#   req={params: {}}      → name="anon"  (dict.get ritorna None)
#   req={params:{name:x}} → name=x       (oggetto OR dict, indifferente)
```

`?.` e `??` insieme = costruzione defensive senza boilerplate.

---

## 2. Strutture dati

```cobra4
# Lista
xs = [1, 2, 3]
xs = [1, 2, 3,]                     # virgola finale OK

# Dict (keys: NON sono interpolazione, sono espressioni cobra4)
d = {"name": "ada", "age": 36}
d = {1: "one", 2: "two"}            # key non-string OK

# Set
s = {1, 2, 3}                       # NON {} (è dict vuoto)
s_empty = set()                     # set vuoto

# Tuple
t = (1, 2, 3)                       # tuple di 3 elementi
t1 = (1,)                           # tuple di 1 elemento (virgola obbligatoria)
empty = ()                          # tuple vuota

# Slice
xs[0]                               # indexing
xs[1:5]                             # slice [start:stop]
xs[:10]                             # primi 10
xs[-3:]                             # ultimi 3
xs[::2]                             # ogni 2 → NON SUPPORTATO M5: usa xs[0::2]
xs[0:10:2]                          # con step
```

### Limiti

- `xs[::2]` con start vuoto + step richiede `xs[0::2]` esplicito.
- Niente "starred" assignment a livello statement (`a, *rest, b = xs` ok in Python; in cobra4 funziona solo se a livello di tuple unpacking standard).

---

## 3. Statement

### 3.1 Assegnamento

```cobra4
x = 1
a, b = 1, 2
a.b = 1                       # attribute assign
xs[0] = 99                    # index assign
counter += 1                  # aug-assign (tutti gli aug-assign Python)
```

**Niente type annotation a livello di assegnamento**: né `x: int` né
`x: int = 0` sono syntax valida — il `:` come separatore di tipo
esiste *solo* su parametri di funzione e return type. Per esprimere
un tipo, usalo nella signature:

```cobra4
fn make_count() -> int = 0
counter = make_count()         # type-checker: counter: int
```

Oppure assegna direttamente con un literal del tipo voluto: l'inferenza
copre `int`, `float`, `str`, `bool`, `list[T]`, `dict[K,V]`, `tuple[...]`,
`set[T]`.

### 3.2 If / elif / else

```cobra4
if x > 0 {
    log("positive")
} elif x == 0 {
    log("zero")
} else {
    log("negative")
}
```

`if` è solo statement. Per espressioni condizionali usa `expr if cond else expr`.

### 3.3 While

```cobra4
while not done {
    process()
    if should_stop {
        break
    }
}
```

`break` e `continue` come Python.

### 3.4 For

```cobra4
for x in xs {
    process(x)
}

# con filtro `where` (M2+):
for x in xs where x > 0 {
    process(x)
}
```

**`for` è solo statement.** Per produrre liste, usa `each` (sotto).

### 3.5 Each (statement E espressione)

```cobra4
# Statement (sequenziale, scarta valori):
each x in xs { process(x) }                        # alias di for

# Espressione (ritorna lista):
results = each x in xs { x * 2 }                   # = list comprehension
filtered = each x in xs where x > 0 { x * 2 }

# Parallelo (fan-out su thread pool, ritorna lista):
results = each url in urls in parallel { fetch(url) }
results = each url in urls in parallel(workers=50) { fetch(url) }
results = each i in big_list in parallel(mode="process") { compute(i) }
```

**Quando usare `each` vs `for`**:

- vuoi i risultati in una lista → `each`
- vuoi solo side-effect → `for`
- vuoi parallelizzare → `each ... in parallel`
- Per filtrare prima di applicare: `each x in xs where cond { f(x) }`.

### 3.6 Match (pattern matching)

```cobra4
match status {
    case 200 { handle_ok(body) }
    case 404 | 410 | 451 { handle_gone() }     # OR-pattern
    case x if x >= 500 { retry() }              # guard
    case _ { log.warn("unexpected", code=status) }
}

# Letterali, name binding, list pattern
match value {
    case None { return }
    case [first, *rest] { ... }                 # rest pattern in list
    case [a, b, c] { ... }                      # exact length
    case (method, path) { ... }                 # tuple destructure
    case Point(x, y) { ... }                    # constructor pattern
    case {"key": v, **other} { ... }            # dict pattern with rest
    case s if s.startswith("http") { ... }      # guard
}
```

**Limiti**: niente range pattern (`case 1..10`).

### 3.7 Try / catch / finally

```cobra4
try {
    risky()
} catch ValueError as e {
    log.error("bad input", err=str(e))
} catch Exception as e {
    log.error("generic", err=str(e))
} finally {
    cleanup()
}
```

`catch` è cobra4; in Python diventa `except`. `as e` lega l'eccezione.

### 3.8 Cron / events

```cobra4
# every Duration { body }
every 30 seconds { check_health() }
every 5 minutes { rotate_logs() }
every 1 hours { backup() }
every 1 days { archive() }

# on event from <source> { body }   — `event` è bound nel body
on event from queue("orders") {
    charge(event.amount)
}
```

**Importante**: in `c4 run`, gli `every`/`on event` registrano callback ma
**non** girano in loop. Usa `c4 serve FILE` per il daemon. Per testing,
`cobra4.runtime.core.run_scheduled_once()` invoca una volta sola.

### 3.9 Serve / Deploy

```cobra4
fn handler(req) {
    name = req?.params?.name ?? "world"
    return {"hello": name}
}

serve handler on :8080

deploy handler to aws.lambda(region="eu-west-1", role="arn:aws:iam::...:role/lambda") {
    env from ".env"
}
```

`serve` registra un'handler HTTP (parte con `c4 serve`). `deploy` di
default è dry-run (logga il piano) — set `COBRA4_DEPLOY_DRY_RUN=0` per
deploy reale.

### 3.10 Use (import)

```cobra4
use json                             # `import json`
use json as j                        # `import json as j`
use cobra4.stdlib.http as http       # cobra4 stdlib via import hook
use "./mylib"                        # path-style (rare)
use github.com/user/lib@1.2          # — NON ANCORA: c4 deps è M7
```

---

### 3.11 Async / await

```cobra4
use asyncio

async fn fetch(url) {
    await asyncio.sleep(0.01)
    return "data: {url}"
}

async fn main() {
    a = await fetch("a")
    b = await fetch("b")

    # `each ... in parallel` dentro un async fn usa asyncio.gather
    # con un semaphore di workers, non un thread pool.
    results = each u in ["x", "y", "z"] in parallel(workers=10) {
        await fetch(u)
    }
    log("done", n=len(results))
}

asyncio.run(main())
```

**Regole**:

- `async fn` produce una coroutine Python.
- `await EXPR` legale solo dentro `async fn`.
- Dentro `each ... in parallel { await coro(x) }` async, l'`await`
  più esterno è ridondante (lo fa il runtime) — il codegen lo strippa.
  Puoi scrivere indifferentemente con o senza.
- `each ... in parallel` fuori da `async fn` continua a usare il
  thread pool. Stesso linguaggio, due implementazioni di concorrenza
  scelte automaticamente.

### 3.12 Result types e operatore `?`

`Ok(value)` e `Err(error)` sono dataclass built-in. L'operatore
postfix `?` propaga l'`Err`:

```cobra4
fn parse_int(s) {
    try {
        return Ok(int(s))
    } catch ValueError as _ {
        return Err("not a number: {s}")
    }
}

fn add_two(a, b) {
    x = parse_int(a)?      # se Err, la fn ritorna subito quell'Err
    y = parse_int(b)?
    return Ok(x + y)
}

match add_two("3", "abc") {
    case Ok(v)  { log("sum", v=v) }
    case Err(e) { log("err", e=e) }
}
```

**Regole**:

- `?` esiste solo come postfix expression (`expr?`).
- Distinto dall'operatore `?.` (safe-nav) — sono due token diversi.
- Una fn che usa `?` viene auto-wrappata in try/except interno:
  l'`Err` propagato diventa il return value della fn.
- Se applicato a qualcosa che non è `Ok` o `Err` → `TypeError` a
  runtime.

### 3.14 Effetti / capability (`with [...]`)

Una funzione può dichiarare quali effetti laterali può generare:

```cobra4
fn pure_double(x) -> int with [] = x * 2

fn fetch_user(id) with [http] = http.get("https://api/{id}")

fn pipeline(id) with [http, log] {
    fetch_user(id)
    log("done")
}
```

**Effetti riconosciuti**: `http`, `fs`, `db`, `log`, `secret`, `ssh`,
`time`, `deploy`. Built-in che hanno effetti dichiarati: `read`/`save`
→ `[fs]`, `log` → `[log]`, `fetch` → `[http]`, `secret` → `[secret]`,
`run` → `[ssh]`, `queue` → `[time]`, `deploy` → `[deploy]`.

**Regole**:

- `with []` = funzione pura.
- `with [a, b]` = caller deve avere `[a, b]` (o superset).
- Senza `with`, la funzione è "unannotated" e nessun controllo viene
  fatto sulle sue chiamate. Adottabile gradualmente.
- Violazione → warning `E001` da `c4 check`. Non blocca esecuzione
  (per ora — runtime sandbox è in roadmap, vedi
  [RFC 0001](docs/rfc/0001-effect-system.md)).

### 3.15 Workflow / task DAG

`workflow NAME { ... }` definisce una pipeline di task con dipendenze
implicite e retry/timeout per task:

```cobra4
fn fetch() = read("./data.csv")
fn clean(rows) = each r in rows where r["age"] >= 18 { r }
fn enrich(rows) = each r in rows in parallel(workers=10) { add_geo(r) }

workflow daily_etl {
    raw       = task fetch(retries=3, timeout=60)
    cleaned   = task clean(raw)
    enriched  = task enrich(cleaned)
    persisted = task save_to_disk(enriched, "./out.parquet")
}

# Dopo il blocco, ogni task var è disponibile come variabile normale
log("done", n=len(enriched), path=persisted)
```

**Regole**:

- Il body può contenere SOLO `var = task EXPR` (no control flow per ora).
- Le dipendenze sono dedotte dai `Name` referenziati nei call args:
  `task clean(raw)` → arc da `raw` a `cleaned`.
- Modifier su `task ...` (kwargs `retries`, `timeout`, `on_failure`)
  vengono estratti al codegen e passati al runner, non alla fn user.
- Cycle detection / DAG topo-sort sono gestiti dal runner.
- Vedi [RFC 0002](docs/rfc/0002-workflow-orchestration.md) per le
  evoluzioni (distributed execution, conditional branches).

### 3.16 Resources / IaC dichiarativo

`resource NAME = adapter.path { field: expr ... }` dichiara
infrastruttura. **Non eseguito da `c4 run`** — usa
`c4 infra plan|apply|destroy FILE`:

```cobra4
resource manifest = local.file {
    path: "./manifest.json"
    contents: {"version": 1, "items": [1, 2, 3]}
}

resource derived = local.file {
    path: "./derived.txt"
    contents: "based on: {manifest.path}"
}
```

```bash
c4 infra plan ./infra.c4       # diff vs ./.cobra4/state.json
c4 infra apply ./infra.c4      # esegue, salva stato
c4 infra destroy ./infra.c4    # tear down, ordine inverso
```

**Adapter built-in**: `local.file` (write JSON/text/bytes). Aggiungerne
di nuovi: `cobra4.runtime.infra.register_adapter("aws.s3", MyS3Adapter())`.
Schema adapter: `plan(current, desired) -> Action`,
`apply(current, desired) -> dict`, `destroy(current) -> None`.

Cross-reference (`derived` legge `manifest.path`) funziona sia in plan
che in apply — il runtime popola `r.state` con i desired prima di
processare il next.

Vedi [RFC 0003](docs/rfc/0003-infra-as-code.md) per la roadmap (più
adapter, S3 state backend, drift detection).

### 3.17 LLM agents (`lang use llm` + `agent`)

```cobra4
lang use llm
use asyncio
use cobra4.runtime.llm as _llm

fn lookup_order(order_id: str) -> str {
    "Look up an order in the system."
    return "order {order_id}: shipped 2026-05-08"
}

agent customer_support(query: str) -> str with [http, log] {
    tools: [lookup_order]
    model: "claude-sonnet-4-6"
    max_iters: 5
    system "You are a concise support agent."
    prompt "Customer question: {query}"
}

# Production: _llm.set_provider(_llm.AnthropicProvider())
# Tests / offline: _llm.MockProvider(scripted=[Response(...), ...])
_llm.set_provider(_llm.AnthropicProvider())

answer = asyncio.run(customer_support("where is order 123?"))
```

`agent NAME(args) -> ret { ... }` is rewritten by the plugin to an
`async fn` body that calls the runtime tool-loop. The fn signature is
preserved verbatim (so type checking + effect annotations + `with`
clauses work as expected).

**Field semantics**:

- `tools: [...]` — list of cobra4 functions. Their docstring (first
  line) becomes the tool description. Type hints become the JSON
  schema. Sync and async tools both work.
- `model: "..."` — provider-specific model name. Default
  `claude-sonnet-4-6`.
- `max_iters: N` — abort the loop if the LLM keeps requesting tools
  past N rounds. Default `10`.
- `system: "..."` — optional system prompt.
- `prompt "..."` — required. **Cobra4 string interpolation handles
  parameter substitution at agent-call time** (`{query}` in the
  template is replaced by the agent's `query` arg) — the runtime never
  templates server-side.

**Provider abstraction**: `cobra4.runtime.llm` exposes
`set_provider(...)`, with built-in `AnthropicProvider` (requires
`pip install anthropic` + `ANTHROPIC_API_KEY`) and `MockProvider`
(scripted responses for tests).

### 3.13 Streaming

Il modulo `cobra4.runtime.stream` fornisce primitive async per
pipeline di eventi:

```cobra4
use asyncio
use cobra4.runtime.stream as s

async fn pipeline() {
    # tumbling window 5s su una coda di eventi
    batches = await s.from_queue("orders").window(tumbling=5.0).collect()
    each b in batches { process(b) }
}
```

Sources: `from_iter(xs)`, `from_async(aiter)`, `from_queue(name)`.
Operators: `.map(fn)`, `.filter(pred)`, `.take(n)`,
`.window(size=N | tumbling=S | sliding=S, step=S)`, `.collect()`,
`.for_each(fn)`. `Stream` è chainable; `.collect()` ritorna `list`,
`.for_each()` consuma side-effect.

## 4. Funzioni

```cobra4
# Inline (single expression)
fn double(x) = x * 2
fn greet(name: str) -> str = "hello {name}"

# Block
fn sum(xs: list) -> int {
    "Docstring (first string is treated as doc by `c4 doc`)."
    total = 0
    for x in xs {
        total += x
    }
    return total
}

# Default args
fn greet(name="world") = "hello {name}"

# Keyword-only via **kwargs
fn build(**opts) {
    log("opts", **opts)
}

# Lambda anonime
double = fn(x) = x * 2
adder = fn(a, b) { return a + b }
```

**Decoratori**: prefix `@`, una per riga, prima della signature:

```cobra4
@smart
fn process(target) { return target }

@cache(ttl=60)
fn fetch_user(id) { ... }
```

**Vincolo M5 lambda block**: una `fn(x) { ... }` con più statement non
sempre transpilable a `lambda` Python (Python lambda è single-expr).
Usa lambda inline `fn(x) = expr` quando possibile, o passa una funzione
nominata.

---

## 5. Classi

```cobra4
class User {
    fn __init__(self, name, age) {
        self.name = name
        self.age = age
    }

    fn is_adult(self) -> bool = self.age >= 18

    fn __repr__(self) = "User({self.name}, {self.age})"
}

# Subclass
class Admin(User) {
    fn __init__(self, name, age, perms) {
        super().__init__(name, age)
        self.perms = perms
    }
}
```

**`data class` shorthand**: dataclass concisa con `__init__` /
`__eq__` / `__repr__` generati:

```cobra4
data class Point(x: int, y: int = 0)
data class User(name: str, email: str)

p = Point(3)            # → Point(x=3, y=0)
u = User("ada", "ada@x.io")
```

I campi senza default precedono quelli con default — l'ordine è
auto-corretto dal codegen rispetto a quello che scrivi.

**`data` per sum types** (varianti taggate, alternativa a Enum):

```cobra4
data Event {
    OrderPlaced(id: str, total: float)
    OrderRefunded(id: str, reason: str)
    OrderShipped(id: str)
}

ev = OrderPlaced(id="x", total=42.5)
match ev {
    case OrderPlaced(id, total)   { ... }
    case OrderRefunded(id, reason){ ... }
    case OrderShipped(id)         { ... }
}
```

Ogni variante è una dataclass figlia della classe base — il pattern
matching usa il pattern `Constructor(args)` esistente.

---

## 6. Smart dispatch — il cuore di cobra4

### 6.1 Modello mentale

Una `SmartFn` è una funzione "aperta" con una catena ordinata di handler.
Ogni handler dichiara su cosa matcha (tipo, scheme URI, estensione, MIME,
predicate custom). La risoluzione sceglie il **più specifico**; tie a
stessa specificità → `AmbiguousDispatch`.

Nascono come built-in (`read`, `save`, `run`) o decorando funzioni utente:

```cobra4
@smart
fn process(target) { return target }   # default fallback

# Aggiungere handler
process.register(fn=fn(x) = ... , type=str, scheme="s3")
process.register(fn=fn(df) = ... , type=DataFrame)
```

### 6.2 Funzioni smart built-in

| Smart fn | Cosa dispatcha su | Esempi |
|---|---|---|
| `read(target)` | scheme URI + ext (file, http(s), s3) | `read("./x.json")`, `read("https://...")`, `read("s3://b/k.csv")` |
| `save(value, target)` | target URI + ext | `save(rows, "out.parquet")` |
| `run(cmd, host=h)` | host (local vs remote) | `run("uptime", host=h)` |

`read` ritorna **automaticamente il tipo giusto**:
- `.csv` → `list[dict]`
- `.json` → dict / list
- `.jsonl` → `list[Any]`
- `.txt`, `.md`, qualsiasi altra estensione locale → `str`
- HTTP con `Content-Type: application/json` → dict
- HTTP con `Content-Type: text/csv` → `list[dict]`

### 6.3 Estendere built-in da codice cobra4

```cobra4
use yaml as _yaml

read.register(
    fn=fn(target) = _yaml.safe_load(open(target).read()),
    type=str, scheme="file", ext="yml",
)
```

### 6.4 Tracing

`COBRA4_TRACE_DISPATCH=1` mostra una riga per dispatch su stderr. Usalo
per capire perché una `read("...")` ha matched l'handler X invece di Y.

### 6.5 Regole importanti

- **Cache per `(type, scheme, ext, mime)`**: se nessun handler usa
  `when=`, le risoluzioni sono cachate. Se almeno uno usa `when=`, la
  cache è disattivata per quella SmartFn (è una decisione di sicurezza,
  evita match silenti errati).
- **Niente `register(scheme="s3")` da solo**: combinalo con `type=str`
  altrimenti finisce in conflict con altri tipi. Il pattern canonico è
  `type=str, scheme="...", ext="..."`.
- **Tie a stessa specificità → errore esplicito**, non fallback.

---

## 7. Cloud primitives

### 7.1 IO smart (read/save)

```cobra4
data = read("./users.csv")                        # list[dict]
save(data, "./users.parquet")                     # parquet via pyarrow

# HTTP
config = read("https://example.com/config.json")  # dict (auto da Content-Type)

# S3 (richiede pip install cobra4[aws])
rows = read("s3://my-bucket/data.csv")
save(rows, "s3://my-bucket/out.parquet")
```

### 7.2 Fleet — comandi remoti

`cobra4.toml` per inventory:

```toml
[hosts.web1]
addr = "10.0.0.1"
user = "deploy"

[hosts.db1]
addr = "10.0.1.1"
user = "ops"

[groups]
prod = ["web1", "db1"]
```

```cobra4
hosts = inventory("prod")     # → [Host(name="web1",...), Host(name="db1",...)]
hosts = inventory("web*")     # glob
hosts = inventory("all")

# Esecuzione
result = run("uptime", host=hosts[0])    # CommandResult(stdout, stderr, returncode, ok)
results = each h in hosts in parallel { run("df -h", host=h) }

# Shell features (solo se serve davvero)
result = run("ls -la | grep .csv | wc -l", host=h, shell=True)
```

**Sicurezza**: `run` è `shell=False` di default. Se passi una stringa,
viene splittata con `shlex.split` ed eseguita come argv. Niente shell
injection. Per pipe/redirect/glob, scegli esplicitamente `shell=True`.

**SSH host keys**: paramiko usa `RejectPolicy` di default — il host deve
essere in `~/.ssh/known_hosts`. Per ambienti effimeri, usa
`Host(extra={"host_key_policy": "auto"})` o `COBRA4_SSH_HOST_KEY_POLICY=auto`.

### 7.3 Secrets

```cobra4
db_pass = secret("postgres/prod/password")
api_key = secret("stripe/api_key")
```

Backend selezionato da `COBRA4_SECRETS_BACKEND`:

| Backend | Lookup | Setup |
|---|---|---|
| `env` (default) | `COBRA4_SECRET_<UPPER_PATH>` | nessuno |
| `file` | `~/.cobra4/secrets/<path>` o `secrets.toml` | `mkdir`, scrivi file |
| `vault` | HashiCorp Vault KV v2 | `pip install hvac`, `VAULT_ADDR`, `VAULT_TOKEN` |
| `aws-sm` | AWS Secrets Manager | `cobra4[aws]`, AWS creds |
| `gcp-sm` | GCP Secret Manager | `google-cloud-secret-manager`, ADC |

### 7.4 Deploy

```cobra4
deploy api_handler to aws.lambda(
    region="us-east-1",
    name="my-api",
    role="arn:aws:iam::123:role/lambda",
    memory=512,
    timeout=10,
) {
    env from ".env"
}
```

**Default = dry-run** (logga il piano, non tocca AWS). Set
`COBRA4_DEPLOY_DRY_RUN=0` per deploy reale. Adapter disponibili:
`aws.lambda` (con vero packaging zip), `gcp.run`, `k8s` (stub), `fly`
(stub). Aggiungere altri:

```cobra4
use cobra4.runtime.deploy as d
d.register_adapter("railway", my_railway_fn)
```

### 7.5 Scheduling / events

```cobra4
state = {"runs": 0}

every 5 minutes {
    log("ingesting")
    state["runs"] += 1
    ingest_batch()
}

on event from queue("orders") {
    log("order", id=event.id)
    process(event)
}
```

Esegui con `c4 serve file.c4` per il daemon vero. Senza, usa
`from cobra4.runtime import core; core.run_scheduled_once()` o
`run_scheduled_for(seconds)` per testing.

### 7.5b Queue backend per `on event from queue(...)`

`queue("name")` ritorna un `EventSource` la cui implementazione concreta
dipende da `COBRA4_QUEUE_BACKEND`:

| Backend | `COBRA4_QUEUE_BACKEND` | Setup |
|---|---|---|
| `InMemoryQueue` (default) | `memory` o non settato | nessuno |
| `FileQueue` (durable, restart-safe) | `file` | `COBRA4_FILE_QUEUE_DIR=...` (default `~/.cobra4/queues/`) |
| `SQSQueue` | `sqs` | `cobra4[aws]`, AWS creds, e il `name` deve essere un nome o ARN di queue SQS |
| `RedisQueue` | `redis` | `pip install redis` + `COBRA4_REDIS_URL=redis://host:6379/0` |

```cobra4
on event from queue("orders") {
    log("order", id=event.id)
    process(event)
}
```

Per produrre eventi a mano (test / boot):

```cobra4
use cobra4.runtime.schedule as sched
sched.queue("orders").put({"id": "abc-123", "total": 42.0})
```

### 7.6 Observability

```cobra4
log("event happened", user=u.id, latency_ms=12)
log.warn("slow", endpoint="/api/x", ms=2400)
log.error("failed", reason="timeout")
```

Output default: `TIMESTAMP level=info msg="..." key=value ...` su stderr.
`COBRA4_LOG_FORMAT=json` per JSON-line. `COBRA4_OTEL_EXPORT=1` (con
`cobra4[otel]`) per export OTLP.

### 7.7 HTTP server (post-#10 hardening)

L'handler riceve un `Request` con: `method`, `path`, `params`, `headers`
(lower-cased), `body` (bytes). Metodi: `req.json()`, `req.text()`.

Convenzioni di ritorno:

```cobra4
fn api(req) {
    # dict / list → 200 JSON
    if req.path == "/health" { return {"ok": True} }

    # str → 200 text/plain
    if req.path == "/version" { return "v1.0" }

    # tuple (status, headers, body) → controllo totale
    if req.path == "/redirect" {
        return (302, {"location": "/new"}, "")
    }

    # tuple (status, body) → status custom + body type-inferred
    return (404, {"error": "not found"})
}
```

Bind default: `127.0.0.1`. Per esporre su rete, set `COBRA4_HTTP_BIND=0.0.0.0`.

---

## 8. Plugin di linguaggio

### 8.1 Attivazione e plugin standard

`lang use NAME` deve essere all'inizio del file (prima di qualsiasi
statement). Plugin disponibili:

| Plugin | Uso | Cosa fa |
|---|---|---|
| `sql` | `sql { SELECT ... }` | Embed SQL. Configura con `sql.configure(url)` o `COBRA4_SQL_URL` per esecuzione **vera** via SQLAlchemy. |
| `regex` | `re"pattern"flags` | Compila a `re.compile` |
| `yaml` | `yaml"""..."""` | Parse YAML al load time, ritorna dict/list |

**Esempio SQL reale**:

```cobra4
lang use sql

use cobra4.plugins.builtin.sql as _sql
_sql.configure("sqlite:///./app.db")    # o postgresql+psycopg, mysql+pymysql, ...

sql_run("CREATE TABLE IF NOT EXISTS users (id TEXT, age INT)")
adults = sql_run("SELECT id FROM users WHERE age >= :min", params={"min": 18})
```

```cobra4
lang use regex
lang use sql

p = re"\d{3}-\d{4}"i
matches = each line in lines where p.search(line) { line }

users = sql {
    SELECT id, name FROM users WHERE active = true
}
```

### 8.2 Tooling è plugin-aware

- `c4 fmt` preserva `lang use` e i blocchi plugin verbatim.
- `c4 check` non flagga i builtin del plugin (es. `sql_run`) come undefined.
- `c4 run`/`c4 build` applicano il preprocessing prima di parser.

---

## 9. Struttura di un progetto cobra4

```
my-project/
├── cobra4.toml              # config: deps, plugins, secrets backend
├── README.md
├── src/
│   ├── main.c4              # entry point (cobra4 module top-level)
│   ├── handlers.c4          # HTTP handlers
│   ├── jobs.c4              # cron / event consumers
│   └── lib/
│       ├── auth.c4          # logical module
│       └── billing.c4
├── tests/
│   └── test_jobs.c4         # cobra4 tests (pytest-equivalent in roadmap)
└── deploy/
    └── deploy.c4            # `deploy ... to ...` script
```

### 9.1 cobra4.toml — esempio completo

```toml
[project]
name = "my-service"
version = "0.1.0"

[deps]
requests = "2.31.0"
boto3 = "*"

[lang]
plugins = ["sql", "regex"]

[secrets]
backend = "vault"

[hosts.web1]
addr = "10.0.0.1"
user = "deploy"

[groups]
prod = ["web1"]
```

### 9.2 Comandi tipici

```bash
# Sviluppo
c4 run src/main.c4                       # transpile + esegue
c4 run src/main.c4 --watch               # re-run on file change
c4 check src/main.c4 --strict            # tipi + linting plugin-aware
c4 fmt src/main.c4 -w                    # format in-place, preserva plugin syntax
c4 repl                                  # REPL multilinea con history + tab
c4 doc src/main.c4                       # markdown da docstring
c4 doc src/main.c4 --html -o docs/main.html

# Daemon (cron + on event + serve)
c4 serve src/main.c4

# Test
c4 test                                  # discover + run tests/test_*.c4
c4 test --verbose --junit-xml=junit.xml  # CI-friendly

# Transpile a Python (per audit / CI)
c4 build src/main.c4 -o build/main.py

# Dipendenze
c4 deps add requests --version 2.31      # aggiorna [deps] in cobra4.toml
c4 deps install                          # pip install (system)
c4 deps install --venv                   # in ./.cobra4/venv (project-local)

# Plugin
c4 plugin list
c4 plugin add sql                        # pip install cobra4-lang-sql
c4 plugin add git+https://github.com/x/cobra4-lang-foo  # da git URL

# Language server (per VS Code / Neovim / Helix)
c4 lsp                                   # diagnostics, format, hover,
                                          # go-to-def, references, symbols, completion
```

### 9.3 Import di moduli locali

cobra4 ha un import hook **generico** installato all'avvio. Questo
significa che `use NAME` o `use a.b` cerca:

1. Plugin di linguaggio (`lang use NAME`) — caricati al pre-process.
2. `cobra4.stdlib.NAME` — finder dedicato per la stdlib.
3. **`<entry>/NAME.c4` su `sys.path`** — finder generico per moduli locali.
4. Pacchetto Python regolare.

```cobra4
# In src/auth.c4:
fn verify(token) = token == "secret"

# In src/main.c4:
use auth                       # carica src/auth.c4
print(auth.verify("secret"))
```

I moduli `.c4` caricati sono cachati in `__pycache__/<name>.cobra4.pyc`
con chiave mtime+size — niente re-parsing finché non modifichi il sorgente.

### 9.4 Layout per Lambda deploy

Quando deploy fa AWS Lambda, il runtime cobra4 viene **vendored** dentro
il zip. Ogni Lambda è autocontenuta. Path handler:
`module.func` (es. `main.api`). L'evento Lambda è wrappato in
`Request(params=event["queryStringParameters"], body=event["body"], headers=event["headers"])`.

---

## 10. Scrivere stdlib in cobra4

La stdlib vive in `cobra4/stdlib/*.c4` e viene caricata da `cobra4.stdlib.<name>`
via import hook. Caratteristiche:

- **Mtime cache**: prima `import` transpila e salva
  `__pycache__/<name>.cobra4.pyc`. Successivi import sono no-op se mtime
  invariato.
- **Niente prefix speciale**: scrivi cobra4 normale; ha accesso a tutti
  i runtime helper.
- **Limite**: niente import di altri stdlib `.c4` (per ora — circular
  resolution non gestita).

Esempio pattern stdlib:

```cobra4
# cobra4/stdlib/retry.c4
"Retry helpers — wrap a callable with backoff."

use time
use random

fn with_retry(fn, max_attempts=3, backoff=0.5) {
    "Run fn with exponential backoff, raise the last exception on giveup."
    last_exc = None
    for attempt in range(max_attempts) {
        try {
            return fn()
        } catch Exception as e {
            last_exc = e
            sleep_for = backoff * (2 ** attempt) + random.random() * 0.1
            log.warn("retry", attempt=attempt + 1, sleep=sleep_for)
            time.sleep(sleep_for)
        }
    }
    raise last_exc
}
```

Uso da utente:

```cobra4
use cobra4.stdlib.retry as retry

result = retry.with_retry(fn() = read("https://flaky.api.com/data"))
```

---

## 11. Errori di sintassi comuni (DON'Ts)

| ❌ Errore | ✅ Corretto |
|---|---|
| `if x > 0:` (Python style) | `if x > 0 { ... }` |
| `def foo(): ...` | `fn foo() { ... }` o `fn foo() = expr` |
| `[x*2 for x in xs]` | `each x in xs { x * 2 }` |
| `{x: x*2 for x in xs}` | usa Python via `use` o costruzione esplicita |
| `lambda x: x + 1` | `fn(x) = x + 1` |
| `f"hello {n}"` | `"hello {n}"` (sempre interpolata) |
| `xs[::2]` | `xs[0::2]` (M5 limitation) |
| `try/except` | `try { ... } catch E as e { ... }` |
| `import X` | `use X` |
| `x: int` (annotation only) | `x: int = 0` |
| dict literal multi-line con newline interni | usa `\` line continuation o stai inline |
| `{}` come empty set | `set()` (la regola: `{}` è sempre dict vuoto) |
| operatore `\|>` | method chaining `xs.filter(...).map(...)` o `each` |

### 11.1 Block vs dict

Il parser disambigua dal contesto:

```cobra4
if cond { foo() }            # block (dopo `cond`, expr completa)
fn x() { return 1 }          # block (dopo signature)

x = {a: 1, b: 2}             # dict (in posizione expression)
fn() { return {a: 1} }       # dict dentro return → expression
```

Se vedi un parse error con `{` su una linea che dovrebbe essere
expression, probabilmente il parser sta tentando di interpretarlo come
block. Wrap in parens: `f({"a": 1})` invece di `f {"a": 1}`.

---

## 12. Esempi realistici end-to-end

### 12.1 Pipeline ETL multi-source

```cobra4
# src/etl.c4 — legge da S3, filtra, arricchisce con HTTP, salva in parquet

use cobra4.stdlib.http as http

fn enrich(row) {
    "Aggiunge geocoding alla riga via API esterna."
    if not row?.address { return row }
    geo = http.get("https://geocode.example/v1?q={row.address}")
    row["lat"] = geo?.lat
    row["lng"] = geo?.lng
    return row
}

# Step 1: read raw
raw = read("s3://prod-data/users-2026-01.csv")
log("raw loaded", count=len(raw))

# Step 2: filter
adults = each row in raw where int(row["age"]) >= 18 { row }
log("adults", count=len(adults))

# Step 3: enrich in parallel (HTTP I/O bound → tante worker)
enriched = each row in adults in parallel(workers=50) { enrich(row) }

# Step 4: persist
save(enriched, "s3://prod-data/users-enriched-2026-01.parquet")
log("etl complete", input=len(raw), output=len(enriched))
```

### 12.2 API REST con auth

```cobra4
# src/api.c4

use json

users_db = {}   # in-memory; produzione → DB reale via runtime

fn require_auth(req) {
    "Estrae user da Bearer token. Ritorna None se invalido."
    auth = req?.headers?.authorization ?? ""
    if not auth.startswith("Bearer ") { return None }
    token = auth[7:]
    return verify_token(token)
}

fn verify_token(token) {
    # In produzione: decode JWT con secret("jwt/signing-key")
    if token == secret("api/dev-token") { return {"id": "ada", "role": "admin"} }
    return None
}

fn handler(req) {
    user = require_auth(req)
    if user is None {
        return (401, {"error": "unauthorized"})
    }

    match (req.method, req.path) {
        case ("GET", "/users") {
            return list(users_db.values())
        }
        case ("POST", "/users") if user["role"] == "admin" {
            payload = req.json()
            users_db[payload["id"]] = payload
            return (201, payload)
        }
        case ("GET", path) if path.startswith("/users/") {
            uid = path[len("/users/"):]
            user_obj = users_db.get(uid)
            if user_obj is None { return (404, {"error": "not found"}) }
            return user_obj
        }
        case _ {
            return (405, {"error": "method not allowed"})
        }
    }
}

serve handler on :8080
```

### 12.3 Health-check distribuito su flotta SSH

```cobra4
# deploy/health.c4

hosts = inventory("prod")
log("checking", n=len(hosts))

fn check_host(h) {
    disk = run("df / | tail -1 | awk '{print $5}'", host=h, shell=True)
    load = run("uptime | awk -F'load average:' '{print $2}'", host=h, shell=True)
    return {
        "host": h.name,
        "ok": disk.ok and load.ok,
        "disk_used": disk.stdout.strip(),
        "load": load.stdout.strip() if load.ok else None,
    }
}

results = each h in hosts in parallel(workers=20) { check_host(h) }

unhealthy = each r in results where not r["ok"] { r }
if len(unhealthy) > 0 {
    log.error("unhealthy hosts", n=len(unhealthy))
    save(unhealthy, "./health-failures.json")
} else {
    log("all healthy", n=len(results))
}
```

### 12.4 Cron + queue consumer + HTTP server (un solo file, run come daemon)

```cobra4
# src/main.c4 — `c4 serve src/main.c4` per avviare tutti e tre

use cobra4.stdlib.http as http

state = {"runs": 0, "errors": 0}

# 1) Job ogni 10 minuti
every 10 minutes {
    log("housekeeping")
    state["runs"] += 1
    try {
        rotate_old_logs()
    } catch Exception as e {
        state["errors"] += 1
        log.error("housekeeping failed", err=str(e))
    }
}

# 2) Consumer su una coda di order
on event from queue("orders") {
    log("order received", id=event.id)
    try {
        process_order(event)
    } catch ValueError as e {
        log.warn("invalid order", id=event.id, err=str(e))
    }
}

# 3) HTTP per status / debug
fn handler(req) {
    match req.path {
        case "/health" { return {"ok": True} }
        case "/metrics" { return state }
        case _ { return (404, {"error": "not found"}) }
    }
}
serve handler on :8080

# Helpers
fn rotate_old_logs() { run("logrotate -f /etc/logrotate.conf") }
fn process_order(o) {
    if not o?.id { raise ValueError("order missing id") }
    log("processing", id=o.id, total=o.total)
}
```

### 12.5 Plugin SQL + secrets per query reale

```cobra4
lang use sql

use cobra4.plugins.builtin.sql as _sql

# Configura il plugin (oppure setta COBRA4_SQL_URL nell'ambiente).
_sql.configure("postgresql://user:{secret('pg/password')}@db.prod:5432/app")

# `sql { ... }` viene riscritto dal plugin a `sql_run("...")`.
adults = sql {
    SELECT id, name, age FROM users WHERE age >= 18 ORDER BY name LIMIT 100
}

log("found", count=len(adults))
for u in adults where u["age"] > 65 {
    log("senior", id=u["id"])
}
```

**Nota**: `with` come statement context-manager **non è supportato** in
cobra4 (non c'è `with x as y { ... }` nella grammatica). Per usare
context manager Python (`open(...)`, `engine.connect()`, …) chiama il
codice Python tramite `use` e gestisci enter/exit con `try`/`finally`,
oppure scrivi una funzione helper Python e importala.

---

## 13. Hooks operativi (env vars)

| Var | Significato |
|---|---|
| `COBRA4_TRACE_DISPATCH=1` | Log ogni risoluzione `SmartFn` |
| `COBRA4_HTTP_BIND` | Bind address daemon HTTP (default `127.0.0.1`) |
| `COBRA4_SSH_HOST_KEY_POLICY=auto\|warn\|strict` | Override paramiko host-key policy |
| `COBRA4_DEPLOY_DRY_RUN=0` | Effettivamente esegue deploy (default = dry-run) |
| `COBRA4_LOG_FORMAT=json\|kv` | Formato output di `log()` |
| `COBRA4_OTEL_EXPORT=1` | Forward log a OTel (richiede `cobra4[otel]`) |
| `COBRA4_SECRETS_BACKEND` | `env`, `file`, `vault`, `aws-sm`, `gcp-sm` |
| `COBRA4_SECRETS_DIR` | Root del backend `file` |
| `COBRA4_LAMBDA_ROLE` | Default IAM role per `aws.lambda` deploy |
| `COBRA4_QUEUE_BACKEND` | `memory` (default), `file`, `sqs`, `redis` — backend di `queue("name")` |
| `COBRA4_FILE_QUEUE_DIR` | Root directory per il `FileQueue` durabile |
| `COBRA4_REDIS_URL` | URL connessione per il `RedisQueue` (`redis://...`) |
| `COBRA4_SQL_URL` | Default URL SQLAlchemy per il plugin `sql` |
| `COBRA4_SECRET_<UPPER_PATH>` | Mapping con backend `env`: `secret("foo/bar")` legge `COBRA4_SECRET_FOO_BAR` |

Setup pacchetti opzionali:

```bash
pip install cobra4[aws]    # boto3
pip install cobra4[data]   # pandas, pyarrow
pip install cobra4[ssh]    # paramiko
pip install cobra4[yaml]   # pyyaml
pip install cobra4[otel]   # OpenTelemetry SDK + exporter
pip install cobra4[dev]    # pytest, black
```

---

## 14. Checklist per l'AI prima di generare cobra4

Prima di scrivere un file `.c4`, verifica:

1. **Niente `:` per aprire blocchi** — cobra4 usa `{ ... }`.
2. **`fn`** non `def`. **`use`** non `import`.
3. Stringhe sono **sempre interpolate** — niente prefix `f`.
4. Niente list comprehension Python (`[x for x in ...]`); usa `each`.
5. Pattern matching: niente `case 1..10` o `case [a, *rest]` in M5.
6. Slice: `xs[start::step]` deve avere `start` esplicito.
7. **`{` dopo signature di control flow** = block. **`{` in posizione expression** = dict.
8. Per dict multi-line: o stai inline, o usa `\` per line continuation.
9. Per `each ... in parallel`: l'iterable può essere un'espressione *senza* `in`. Wrap in parens se serve membership: `each x in (a in xs) { ... }`.
10. **`?.` con dict**: ritorna `.get(key)`, NON `getattr`. È intenzionale.
11. **`save()` è atomico**: scrive a temp + rename. Se il target esiste, viene sostituito atomicamente.
12. **`run()` è `shell=False` di default**: passa lista o stringa shlex-splittabile. Per pipe/redirect/glob, `shell=True` esplicito.
13. **`COBRA4_DEPLOY_DRY_RUN`** è `1` di default — il deploy NON tocca infrastruttura senza opt-in.
14. **HTTP server bind = `127.0.0.1`** di default. Per esporre, set env var.
15. Per generare la lista filter+transform più idiomatica:
    `each x in xs where cond { transform(x) }`.

### 14.1 Quando *non* sai

- Se un costrutto sembra esistere ma non è in questa guida, **non
  inventarlo**. Scrivi una versione "verbose" equivalente con i
  costrutti documentati.
- Se il task richiede un costrutto mancante (esempio: `*rest` pattern),
  scrivi un commento `# TODO M6: ...` e usa il fallback più vicino.
- Per qualsiasi cosa di "fancy" Python (decorator stacking complesso,
  metaclass, descriptor protocol), accedi tramite `use NAME` Python e
  chiama dal lato cobra4 in modo diretto.

---

## 15. File di riferimento nel codice

Quando l'AI deve verificare un comportamento esatto, leggere il sorgente
batte ogni documento. Mappa:

| Domanda | File da leggere |
|---|---|
| "Come si parsa X?" | [grammar.lark](cobra4/grammar.lark), [parser.py](cobra4/parser.py) |
| "Come emette Python il codegen?" | [codegen.py](cobra4/codegen.py) |
| "Cosa fa `read("s3://...")`?" | [runtime/io.py](cobra4/runtime/io.py) |
| "Come funziona `each ... in parallel`?" | [runtime/concurrency.py](cobra4/runtime/concurrency.py) + emit in codegen |
| "Quali handler ha `read`?" | runtime: `read.handlers` (lista ordinata per specificity) |
| "Come è definito un plugin?" | [plugins/api.py](cobra4/plugins/api.py), reference in [plugins/builtin/sql.py](cobra4/plugins/builtin/sql.py) |
| "Cosa fa `c4 serve`?" | [runtime/schedule.py](cobra4/runtime/schedule.py), [cli.py](cobra4/cli.py) `cmd_serve` |
| "Quali built-in conosce il resolver?" | [resolver.py](cobra4/resolver.py) `_PY_BUILTINS` + `_C4_BUILTINS` |
| "Quali env vars?" | sezione 13 di questo file, oppure cerca `os.environ.get` |

Esegui il test suite per validare ogni cambiamento:

```bash
python -m pytest                           # 174 passing (+ 2 skipped)
python -m pytest tests/test_review_fixes.py  # regression sui fix critici
```

Se aggiungi una feature, **aggiungi un test in `tests/`** e una riga in
questo file (sezioni 1–12 a seconda di cosa).
