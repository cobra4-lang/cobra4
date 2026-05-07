# Fleet & secrets

## Fleet — remote command execution

Inventory in `cobra4.toml`:

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

Use:

```cobra4
hosts = inventory("prod")           # → [Host, Host]
hosts = inventory("web*")           # glob
hosts = inventory("all")

result = run("uptime", host=hosts[0])     # CommandResult(stdout, stderr, returncode, ok)
results = each h in hosts in parallel { run("df -h", host=h) }

# Shell features when you need them
result = run("ls -la | grep .csv | wc -l", host=h, shell=True)
```

`run` is **`shell=False`** by default. A string is `shlex.split`-ed and
passed as `argv` — no shell injection surface.

SSH host keys: paramiko uses **`RejectPolicy`** by default. The host
must be in `~/.ssh/known_hosts` or set
`COBRA4_SSH_HOST_KEY_POLICY=auto` for ephemeral environments.

## Secrets

```cobra4
db_pass = secret("postgres/prod/password")
api_key = secret("stripe/api_key")
```

Backends, selected via `COBRA4_SECRETS_BACKEND`:

| Backend          | Lookup                                                       |
|------------------|--------------------------------------------------------------|
| `env` (default)  | `COBRA4_SECRET_<UPPER_PATH>` (slashes → underscores)         |
| `file`           | `~/.cobra4/secrets/<path>` or `secrets.toml`                 |
| `vault`          | HashiCorp Vault KV v2 (`pip install hvac`, `VAULT_*` env)    |
| `aws-sm`         | AWS Secrets Manager (`cobra4[aws]`)                          |
| `gcp-sm`         | GCP Secret Manager (`google-cloud-secret-manager`, ADC)      |
