# Schedule

Built for "two to three weeks, maybe less". The order front-loads a working demo and
delays the Databricks trial until everything around it is ready, so none of the 14
days are spent on setup that did not need Databricks.

If the interview moves up, stop at the end of any stage. Each one leaves you with
something you can show.

## Stage 1: edge and agent (days 1 to 3, no trial running)

| Day | Do | You will understand |
|---|---|---|
| 1 | Apply `infra/01-origin`. Run `local_publish.py`. Deploy the Worker. `curl -i` the endpoint until you can predict `x-cache`. | Origin vs edge, cache keys, TTLs, why the origin is private |
| 2 | Read `edge/src/index.js` and its tests. Change `EDGE_TTL_SECONDS`, redeploy, observe. Run `weather_agent check`. | Soft vs hard TTL, stale-on-error, what a monitor can see from outside |
| 3 | Read `loop.py`, `tools.py`, `prompts.py` in that order. Run `investigate --force`. Run the `worker_error` and `origin_auth` drills and read the reports. | What an agent is: a loop, tools, and a prompt |

**Demo you have:** a live CDN endpoint on your own domain and an agent that diagnoses two real edge failures.

## Stage 2: pipeline (trial days 1 to 4)

| Trial day | Do | You will understand |
|---|---|---|
| 1 | Apply `infra/02-databricks`. Deploy and run the bundle. Fix first-run issues. | Workspaces, Unity Catalog, bundles, serverless jobs |
| 2 | Open the job in the UI. Query the bronze, silver, and `ops_run_log` tables. Check Azure cost. | Medallion layers, why raw data is kept, what a task run looks like |
| 3 | Connect the agent to Databricks. Run `upstream_down` and `schema_drift`. | Jobs API vs system tables, read-only SQL as a tool |
| 4 | Push the repo to GitHub. Get `ci.yml` green. | What your team's pipeline for this would look like |

## Stage 3: drills and evaluation (trial days 5 to 9)

| Trial day | Do |
|---|---|
| 5 | `python evals/drill.py run all`. Expect failures, some from the harness itself. |
| 6 to 7 | For each failed drill, read the report's audit trail and decide which it was: a missing tool, a vague tool description, a prompt gap, or a model limit. Change one thing and rerun that drill. |
| 8 | Run everything once with `AGENT_MODEL` set to a smaller model and once with a larger one. Compare pass rate, tokens, and time. |
| 9 | Try `investigate --allow-actions` on `wrong_path` and on `ttl_misconfig`. Approve one action, decline one. |

The iteration on days 6 and 7 is the most valuable part of the project. Keep notes on
what you changed and why; those notes are your interview material.

## Stage 4: package it (trial days 10 to 12)

- Run `drill.py scorecard` and commit `evals/SCORECARD.md` and the reports.
- Record a five-minute screen capture: healthy check, inject `wrong_path`, agent report, approve the fix. **The workspace stops working when the trial ends**, so this recording is your demo if the interview falls after day 14.
- Write half a page: what worked, what did not, what you would do next.
- Days 13 and 14 are buffer. Then `terraform destroy` the Databricks stack.

After the trial, `local_publish.py` keeps the edge live for a demo at near-zero cost.

## Stretch goals, in order of interview value

1. Replace the hand-written Databricks and Cloudflare tools with their MCP servers and keep the same loop. Shows you know where the industry is standardizing.
2. Run the monitor on a schedule (a GitHub Actions cron) and post reports to Slack.
3. Add a `schedule_stopped` drill: unpause the schedule, then pause it, and see whether the agent distinguishes "no runs" from "runs that misbehave".
4. Add a second Worker route with a cache rule and compare Cache API behaviour with Cloudflare's built-in cache.
