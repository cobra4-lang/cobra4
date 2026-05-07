# Environment variables

| Var                              | Effect                                                                                  |
|----------------------------------|-----------------------------------------------------------------------------------------|
| `COBRA4_TRACE_DISPATCH=1`        | Log every `SmartFn` resolution to stderr.                                               |
| `COBRA4_HTTP_BIND=0.0.0.0`       | Override daemon HTTP bind (default `127.0.0.1`).                                        |
| `COBRA4_SSH_HOST_KEY_POLICY=auto`| Use paramiko `AutoAddPolicy` (default is `RejectPolicy`).                               |
| `COBRA4_DEPLOY_DRY_RUN=0`        | Actually invoke deploy adapters (default = dry-run).                                    |
| `COBRA4_LOG_FORMAT=json`         | Switch `log()` from `key=value` to JSON-line.                                           |
| `COBRA4_OTEL_EXPORT=1`           | Forward log records to OTel (requires `cobra4[otel]`).                                  |
| `COBRA4_SECRETS_BACKEND`         | `env` \| `file` \| `vault` \| `aws-sm` \| `gcp-sm`.                                     |
| `COBRA4_SECRETS_DIR`             | Override the file-backend root (default `~/.cobra4/secrets/`).                          |
| `COBRA4_SECRET_<UPPER_PATH>`     | With backend `env`: lookup destination for `secret("foo/bar")` (slashes → underscores). |
| `COBRA4_LAMBDA_ROLE`             | Default IAM role ARN for `aws.lambda` deploys.                                          |
| `COBRA4_QUEUE_BACKEND`           | `memory` \| `file` \| `sqs` \| `redis`.                                                 |
| `COBRA4_FILE_QUEUE_DIR`          | Where the `FileQueue` stores events (default `~/.cobra4/queues/`).                      |
| `COBRA4_REDIS_URL`               | Connection URL for the `RedisQueue` backend (`redis://...`).                            |
| `COBRA4_SQL_URL`                 | Default SQLAlchemy URL for the `sql` plugin.                                            |
