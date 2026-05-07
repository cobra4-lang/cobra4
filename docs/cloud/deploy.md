# Deploy adapters

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

**Default = dry-run.** `deploy` logs the plan but does not touch your
infrastructure unless `COBRA4_DEPLOY_DRY_RUN=0` is set.

## Built-in adapters

| Adapter        | Description                                              |
|----------------|----------------------------------------------------------|
| `aws.lambda`   | Real packaging: deterministic zip with vendored cobra4 runtime, idempotent create-or-update via boto3. |
| `gcp.run`      | `gcloud run deploy` with a Docker build.                |
| `k8s`          | Generates manifests + `kubectl apply`.                   |
| `fly`          | Calls `flyctl deploy`.                                   |

## Adding your own

```cobra4
use cobra4.runtime.deploy as d
d.register_adapter("railway", my_railway_fn)

deploy api to railway(name="my-app") { env from ".env" }
```
