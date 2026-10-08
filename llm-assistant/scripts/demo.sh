#!/usr/bin/env bash
# Смоук-демо пункта 9: детект (вебхук) → LLM-разбор → audit.
# Запуск: ./scripts/demo.sh [base_url]
set -euo pipefail

BASE="${1:-http://localhost:8080}"

echo "== health =="
curl -sS "$BASE/healthz"
echo

echo "== промты =="
curl -sS "$BASE/prompts"
echo

echo "== демо-инцидент (suggested) =="
curl -sS -X POST "$BASE/demo/incident" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(json.dumps({
        "mode": d["mode"], "severity": (d.get("parsed") or {}).get("severity"),
        "hypotheses": (d.get("parsed") or {}).get("root_cause_hypotheses"),
        "actions": (d.get("parsed") or {}).get("recommended_actions"),
        "warnings": d["warnings"]}, ensure_ascii=False, indent=2))'

echo "== алерт из Alertmanager (sample_alert.json) =="
curl -sS -X POST "$BASE/webhook/alertmanager" \
  -H 'content-type: application/json' \
  --data-binary @sample_alert.json \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); p=d.get("parsed") or {}; print(p.get("summary"), "| severity:", p.get("severity"))'

echo "== предложение по восстановлению =="
curl -sS -X POST "$BASE/remediation/propose" \
  -H 'content-type: application/json' \
  -d '{"symptoms":"5xx растёт после релиза","namespace":"demo","target_name":"demo-app"}' \
  | python3 -c 'import json,sys; p=json.load(sys.stdin).get("parsed") or {}; print("dry_run:", p.get("dry_run")); print("options:", json.dumps(p.get("options"), ensure_ascii=False, indent=2))'

echo "== audit log =="
curl -sS "$BASE/audit?limit=5"
echo
