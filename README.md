# weather-edge

A small, complete version of a weather data platform: a Databricks pipeline on Azure
publishes JSON to blob storage, Cloudflare serves it at `weather.vilnico.com`, and an
AI agent investigates when something breaks.

```
Open-Meteo API -> Databricks job (ingest -> transform -> publish) -> Azure Blob (private origin)
                                                                          |
                                         users <- Cloudflare Worker + edge cache
                                                          ^
        rule-based monitor -> agent (read-only tools) -> incident report -> human-approved fix
```

## The one idea to take away

**Rules detect. The agent diagnoses. A human approves.**

- `agent/weather_agent/detect.py` decides *whether* something is wrong, with plain
  thresholds. It is cheap, fast, and cannot hallucinate.
- `agent/weather_agent/loop.py` is asked *why*. It can only read: HTTP probes, job
  runs, task output, SQL, CDN metrics.
- Two actions exist (rerun the job, purge the cache). They are hidden unless you pass
  `--allow-actions`, and each call still waits for a `y` at the terminal.
- `evals/` breaks the system ten known ways and scores whether the agent names the
  true cause. That scorecard is what turns "we tried an AI agent" into evidence.

## What is in here

| Path | What it is |
|---|---|
| `infra/01-origin` | Terraform: resource group, private storage account, read and write SAS tokens |
| `infra/02-databricks` | Terraform: trial workspace and secret scope. **Applying it starts the 14-day trial** |
| `pipeline/` | Databricks bundle: one job, three notebook tasks, on serverless compute |
| `pipeline/src/weatherlib.py` | The pipeline logic as plain Python, shared by notebooks, laptop, and tests |
| `pipeline/local_publish.py` | Runs fetch and publish from a laptop, so the CDN works before Databricks exists |
| `edge/` | Cloudflare Worker with soft/hard TTL caching and stale-on-error fallback |
| `agent/` | Monitor, tools, guardrails, the agent loop, report writer |
| `evals/` | Ten failure drills, the harness that runs them, and the scorecard |
| `docs/PLAN.md` | Day-by-day schedule built around the trial clock |
| `docs/INTERVIEW.md` | How to talk about this as a manager |

## Status: read this before the first run

Tested here: 44 unit tests pass (10 Worker, 26 agent, 5 pipeline, 3 drill harness), the
Worker's cache states were exercised over HTTP in Cloudflare's local runtime, and
`wrangler deploy --dry-run` accepts the config. The Databricks and Anthropic SDK calls
were checked against the installed SDKs' signatures.

Not tested: anything against live Azure, Databricks, or Cloudflare. Expect to fix a
few things on first contact. The most likely ones:

- **Terraform** was not validated or formatted (the registry is unreachable from where
  this was written). Run `terraform fmt -recursive infra` and `terraform validate` first;
  CI checks both.
- **Cloudflare GraphQL** field names in `clients.py` are from memory. If a query is
  rejected, the error message names the bad field. The monitor treats this as a blind
  spot and carries on.
- **Purge by hostname** on the Free plan: if it is refused, purge by URL instead
  (`purge_edge_cache` accepts paths) or let copies age out.
- **Serverless jobs** must be available in your region and covered by the trial. If
  not, add a small single-node `job_clusters` block to `pipeline/resources/weather_job.yml`.
- **Catalog name**: new Azure workspaces create a catalog named after the workspace,
  not `main`. Pass it with `--var="catalog=..."`.

## Setup

Prerequisites: Azure CLI, Terraform 1.6+, Node 20+, Python 3.11+, the Databricks CLI,
an Anthropic API key, and Cloudflare access to the `vilnico.com` zone. The Azure
subscription must be pay-as-you-go; the Databricks trial is refused on a Free Trial one.

### Phase A: origin, edge, agent (no trial clock)

```bash
az login
cd infra/01-origin && terraform init && terraform apply -var subscription_id=<SUB_ID>

export ORIGIN_CONTAINER_URL=$(terraform output -raw container_url)
export ORIGIN_WRITE_SAS=$(terraform output -raw write_sas)
python ../../pipeline/local_publish.py          # seeds the origin from your laptop

cd ../../edge                                   # put container_url into wrangler.jsonc first
npm install && npx wrangler login
npx wrangler deploy
npx wrangler secret put ORIGIN_SAS              # paste: terraform output -raw read_sas
curl -i https://weather.vilnico.com/v1/index.json     # x-cache: MISS, then HIT

cd ../agent && python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env    # fill in what you have so far
python -m weather_agent check
python -m weather_agent investigate --force
```

`weather.vilnico.com` must not already have a DNS record. Deploying creates the record
and its certificate and leaves every other record on the zone alone.

Two drills already work at this point, with no Databricks: `python evals/drill.py run
worker_error` and `python evals/drill.py run origin_auth` (run from the repo root).

### Phase B: Databricks (starts the trial)

```bash
cd infra/02-databricks && terraform init && terraform apply -var subscription_id=<SUB_ID>
export DATABRICKS_HOST=$(terraform output -raw workspace_url)
databricks auth login --host $DATABRICKS_HOST

cd ../../pipeline
databricks bundle deploy --var="catalog=<your workspace catalog>"
databricks bundle run weather_publish
databricks bundle summary          # job ID for agent/.env
databricks warehouses list         # warehouse ID for agent/.env
```

Then fill in the Databricks block of `agent/.env` and run the full set:

```bash
python evals/drill.py run all      # one to two hours, mostly waiting on job runs
python evals/drill.py scorecard
```

### Phase C: CI

`.github/workflows/ci.yml` runs every test and validation on each push.
`deploy.yml` deploys the Worker and the bundle from `main`. Secrets it needs:
`CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `DATABRICKS_HOST`, `DATABRICKS_TOKEN`.

## Cost guardrails

- The job schedule ships **paused**. Runs happen when you trigger them.
- Tasks run on serverless compute, so there is no cluster to leave running.
- The SQL warehouse the agent queries auto-stops; leave that setting on.
- Open Azure Cost Management on day two of the trial and confirm the number is small.
- Storage, the Worker, and the agent's model calls together should be a few dollars.

## Teardown

```bash
cd infra/02-databricks && terraform destroy -var subscription_id=<SUB_ID>
cd ../01-origin && terraform destroy -var subscription_id=<SUB_ID>
cd ../../edge && npx wrangler delete
```

`terraform.tfstate` contains the SAS tokens. It is in `.gitignore`; keep it there.
