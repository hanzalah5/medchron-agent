# Deployment

MedChron ships as **one container image**. The API also serves the reviewer UI, and the worker runs from the same image with a different command. The reference deployment targets Google Cloud and is designed to cost close to nothing when idle.

```
             HTTPS                          enqueue (Postgres)
 Browser ───────────► Cloud Run service ─────────────────────► Postgres + pgvector
                      (API + UI, scales     │                  (Neon or Cloud SQL)
                       to zero)             │ starts execution        ▲
                                            ▼                         │
                                      Cloud Run job ──────────────────┘
                                      (worker --once: drains the queue, exits)
                      GCS bucket (records) · Secret Manager · Artifact Registry
 GitHub Actions ── OIDC ──► Workload Identity Federation ──► deployer service account
```

## Why this shape

| Choice | Reason |
|---|---|
| Cloud Run service with `min_instance_count = 0` | No traffic means no compute bill |
| Worker as a Cloud Run **job** started by the API | A polling worker would bill around the clock. A job runs only when there is work. The queue lives in Postgres, so a missed trigger loses nothing: the next execution drains it, and stale jobs are recovered |
| Postgres as database, vector store and queue | One managed service to pay for and operate |
| Neon (serverless Postgres with pgvector) for the demo | Scales to zero, and its free tier covers a demo. Cloud SQL is the drop-in alternative for a firm deployment |
| Secret Manager with values added out of band | Secret values never enter Terraform state or the repository |
| GitHub OIDC with Workload Identity Federation | No long-lived cloud keys in GitHub. Only this repository can authenticate |

Free tiers and prices change, so check current provider terms. Set a budget: the module creates a billing alert when `billing_account` is set.

## One-time setup

```bash
# 1. Infrastructure (needs a GCP project and a GCS bucket for Terraform state)
cd infra/terraform/envs/dev
cp terraform.tfvars.example terraform.tfvars   # edit
terraform init -backend-config="bucket=<tf-state-bucket>"
terraform apply
```

```bash
# 2. Secret values (never stored in Terraform)
echo -n "postgresql+psycopg://USER:PASS@HOST/medchron?sslmode=require" | \
  gcloud secrets versions add medchron-dev-database-url --data-file=-
echo -n "$ANTHROPIC_API_KEY" | gcloud secrets versions add medchron-dev-anthropic-api-key --data-file=-
echo -n "$(openssl rand -hex 12)" | gcloud secrets versions add medchron-dev-demo-access-code --data-file=-
```

```bash
# 3. Enable pgvector in the database (once)
psql "$DATABASE_URL" -c "CREATE EXTENSION IF NOT EXISTS vector"
```

```bash
# 4. Database roles (once, and after rotating passwords): run as the owner, which needs CREATEROLE.
#    Migrations (run as the owner) create the group roles and grants; this creates the logins.
DATABASE_URL="$OWNER_URL" MEDCHRON_DB_API_PASSWORD=... MEDCHRON_DB_WORKER_PASSWORD=... \
  medchron db-users
```

The services must **not** connect as the owner. Postgres exempts superusers from row-level security, and an owner can turn it off.

| Role | Used by | Can |
|---|---|---|
| owner (e.g. `medchron`) | Migrations only | Everything; member of `medchron_system` |
| `medchron_api` | API service, MCP server | Read and write rows of matters the caller is a member of. Append to the audit log, never update or delete it. **Cannot** enable the system bypass |
| `medchron_worker` | Worker job | Same, plus the system bypass (`medchron.system`) for background processing |

Store `medchron_api`'s URL in the API's `database-url` secret and `medchron_worker`'s URL in the worker's. Migrations need the owner URL in a separate secret.

The Terraform module has a single `database-url` secret for both services today, so a real rollout must split it into three: API, worker and migration. This change is described here but not applied in `infra/terraform`.

5. GitHub repository variables: `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER` (from `terraform output workload_identity_provider`) and `GCP_DEPLOYER_SA` (from `terraform output deployer_service_account`). For `prod-demo`, add required reviewers under Settings → Environments.

After that, the [deploy workflow](../.github/workflows/deploy.yml) works like this:
- every green CI run on `main` builds and pushes the image, runs migrations through the worker job, deploys the API and smoke-tests `/healthz`
- `prod-demo` deploys only on a manual run behind the environment approval

## Environments

| | `local` | `dev` | `prod-demo` |
|---|---|---|---|
| How | `docker compose --profile app up --build` | Auto-deploy from `main` | Manual and approved |
| Auth | Dev sign-in | Dev sign-in (`MEDCHRON_ENV=demo`) or OIDC | Same as dev |
| LLM runs | Your key | Access code plus per-case budget cap | Access code plus per-case budget cap |
| Default engine | Any | Demo oracle (no spend) | Demo oracle (no spend) |

The public demo is synthetic-only. Visitors choose a display name, the bundled cases run on the **demo oracle** at zero cost, and live Claude runs need an access code and are capped per case (`MEDCHRON_DEMO_MAX_BUDGET_USD`).

## What changes for a real production rollout

| Demo choice | Production |
|---|---|
| Public project, free tiers | The firm's own cloud organization with VPC Service Controls and private networking (Cloud Run behind an internal load balancer or IAP), under BAAs with every vendor that touches PHI |
| Direct Anthropic API | Claude through the firm's cloud (Vertex AI or Bedrock) or the direct API under an appropriate data-retention agreement, with data-residency constraints applied |
| Dev sign-in | `MEDCHRON_AUTH_MODE=oidc` against the firm's IdP (SSO and MFA), SCIM provisioning, matter membership synced from the matter-management system |
| Neon | Cloud SQL (or AlloyDB) with private IP, HA, PITR backups and tested restores. Separate owner, API and worker roles as above (compose and CI already run this way) |
| Google-managed encryption | Customer-managed keys (`kms_key_name`) with rotation, plus per-matter keys for crypto-shredding |
| Audit log in Postgres | Also streamed to WORM storage or a SIEM, with alerting on chain-verification failure |
| Local OCR in the container | Scaled OCR workers or a managed OCR service under a BAA |
| `min_instances = 0` | Minimum instances and SLOs for latency-sensitive use, autoscaling limits and load tests |
| Single region, best effort | Multi-zone, a disaster-recovery plan, incident runbooks and an on-call rotation |
| Self-assessed security | External penetration test, formal risk analysis, vendor reviews, staff training and policies |
| Synthetic evaluation set | A firm-approved de-identified evaluation set, ongoing human QA sampling and model-change governance |
