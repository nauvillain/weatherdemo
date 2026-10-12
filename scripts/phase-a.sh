#!/usr/bin/env bash
# Phase A in one go: origin storage, seed data, Cloudflare Worker, agent check.
# Does not touch Databricks, so the trial clock does not start.
# Safe to re-run: every step either skips or converges on the same result.
#
#   bash scripts/phase-a.sh
#
# It stops for you three times: the Azure login (if needed), Terraform's plan
# (type "yes" to create the resources), and the Cloudflare login (if needed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
EDGE_URL="https://weather.vilnico.com"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing '$1'. Install it with: $2"; exit 1; }; }

need az "curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash"
need terraform "sudo snap install terraform --classic"
need node "sudo apt install nodejs npm"
need npm "sudo apt install npm"
need python3 "sudo apt install python3"
need curl "sudo apt install curl"
python3 -c "import venv, ensurepip" 2>/dev/null || { echo "Missing Python venv support. Install it with: sudo apt install python3-venv"; exit 1; }

step "1/6 Azure login"
az account show >/dev/null 2>&1 || az login || true
if ! SUB_ID="$(az account show --query id -o tsv 2>/dev/null)"; then
  cat <<'EOF'
You are signed in, but this account has no Azure subscription.
Create a pay-as-you-go subscription at https://azure.microsoft.com/pricing/purchase-options/pay-as-you-go
(a Free Trial subscription will not work for the Databricks trial), then run:
  az login
  bash scripts/phase-a.sh
If you already have a subscription in another directory, use: az login --tenant <directory id>
EOF
  exit 1
fi
echo "Subscription: $(az account show --query name -o tsv) ($SUB_ID)"
read -r -p "Create the resources in this subscription? [Y/n] " answer
if [[ ! "${answer:-Y}" =~ ^[Yy]$ ]]; then
  echo "Switch with: az account set --subscription <id>   then run this script again."
  exit 1
fi

step "2/6 Origin storage (Terraform)"
terraform -chdir=infra/01-origin init -input=false
terraform -chdir=infra/01-origin apply -var "subscription_id=$SUB_ID"
CONTAINER_URL="$(terraform -chdir=infra/01-origin output -raw container_url)"
echo "Origin container: $CONTAINER_URL"

step "3/6 Python environment for the agent and the seed script"
[ -d agent/.venv ] || python3 -m venv agent/.venv
agent/.venv/bin/pip install -q --upgrade pip
agent/.venv/bin/pip install -q -r agent/requirements.txt
PY="$ROOT/agent/.venv/bin/python"

step "4/6 Seed the origin with live weather data"
ORIGIN_CONTAINER_URL="$CONTAINER_URL" \
ORIGIN_WRITE_SAS="$(terraform -chdir=infra/01-origin output -raw write_sas)" \
  "$PY" pipeline/local_publish.py

step "5/6 Cloudflare Worker at $EDGE_URL"
sed -i "s#\"ORIGIN_BASE\": \"[^\"]*\"#\"ORIGIN_BASE\": \"$CONTAINER_URL\"#" edge/wrangler.jsonc
(
  cd edge
  npm install --no-audit --no-fund --silent
  if ! npx wrangler whoami 2>&1 | grep -q "You are logged in"; then
    npx wrangler login
  fi
  npx wrangler deploy
  terraform -chdir=../infra/01-origin output -raw read_sas | npx wrangler secret put ORIGIN_SAS
)

echo "Waiting for DNS and the certificate (first deploy can take a few minutes)..."
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "$EDGE_URL/healthz" 2>/dev/null; then break; fi
  sleep 10
done
for attempt in 1 2; do
  echo "Request $attempt:"
  curl -sS -D - -o /dev/null "$EDGE_URL/v1/index.json" | grep -iE "^HTTP|^x-cache:" || true
done

step "6/6 Agent configuration and first check"
[ -f agent/.env ] || cp agent/.env.example agent/.env
ANTHROPIC_KEY=""
if ! grep -qE "^ANTHROPIC_API_KEY=.+" agent/.env; then
  read -r -s -p "Anthropic API key for the agent (press Enter to skip for now): " ANTHROPIC_KEY
  echo
fi
ENV_FILE=agent/.env \
EDGE_BASE_URL="$EDGE_URL" \
ORIGIN_CONTAINER_URL="$CONTAINER_URL" \
ORIGIN_READ_SAS="$(terraform -chdir=infra/01-origin output -raw read_sas)" \
ANTHROPIC_KEY="$ANTHROPIC_KEY" \
"$PY" - <<'EOF'
import os, re
path = os.environ["ENV_FILE"]
updates = {k: os.environ[k] for k in ("EDGE_BASE_URL", "ORIGIN_CONTAINER_URL", "ORIGIN_READ_SAS")}
if os.environ["ANTHROPIC_KEY"]:
    updates["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_KEY"]
text = open(path).read()
for key, value in updates.items():
    line = f"{key}={value}"
    text, n = re.subn(rf"^{key}=.*$", lambda _m: line, text, flags=re.M)
    if not n:
        text += f"\n{line}\n"
open(path, "w").write(text)
EOF
chmod 600 agent/.env
echo "Wrote agent/.env (git ignores it)."

(cd agent && .venv/bin/python -m weather_agent check) || true

cat <<EOF

Phase A is done. Next:
  cd agent && .venv/bin/python -m weather_agent investigate --force
  cd .. && agent/.venv/bin/python evals/drill.py run worker_error
  agent/.venv/bin/python evals/drill.py run origin_auth
EOF
