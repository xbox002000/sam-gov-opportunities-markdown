#!/usr/bin/env bash
# Local end-to-end run with SIMULATED pay-per-event pricing (reads .actor/pay_per_event.json).
# Usage: scripts/local_test.sh [input.json]
set -euo pipefail
cd "$(dirname "$0")/.."
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
. .venv/bin/activate
export ACTOR_TEST_PAY_PER_EVENT=true
export APIFY_CHARGED_ACTOR_EVENT_COUNTS="$(python3 -c "import json;print(json.dumps({k:0 for k in json.load(open('.actor/pay_per_event.json'))}))")"
export APIFY_ACTOR_PRICING_INFO="$(python3 -c "import json;e=json.load(open('.actor/pay_per_event.json'));e.setdefault('apify-default-dataset-item',{'eventTitle':'removed','eventPriceUsd':0});print(json.dumps({'pricingModel':'PAY_PER_EVENT','pricingPerEvent':{'actorChargeEvents':e}}))")"
rm -rf storage/datasets/default storage/datasets/charging-log; find storage/key_value_stores/default -type f ! -name "INPUT*" -delete 2>/dev/null || true
if [ "${1:-}" != "" ]; then
  apify run -p --entrypoint src --input-file "$1"
else
  apify run -p --entrypoint src
fi
python3 scripts/summarize_run.py
