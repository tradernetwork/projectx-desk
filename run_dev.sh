#!/bin/bash
# Offline desk against the mock gateway: no credentials, no real orders. Ctrl+C stops both.
# Usage: ./run_dev.sh   then open http://127.0.0.1:8613/?ai=1
# Other ports (run two at once): CHART_PORT=8623 DEV_GATEWAY_PORT=8710 ./run_dev.sh
cd "$(dirname "$0")"
GWP=${DEV_GATEWAY_PORT:-8700}; APPP=${CHART_PORT:-8613}
if [ -n "$PYTHON" ]; then PY="$PYTHON"; elif [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
D=$(mktemp -d)
DEV_GATEWAY_PORT=$GWP "$PY" dev_gateway.py > "$D/gw.log" 2>&1 & GW=$!
trap 'kill $GW $APP 2>/dev/null' EXIT INT TERM
sleep 2
export PROJECTX_API=http://127.0.0.1:$GWP PROJECTX_HUB_BASE=ws://127.0.0.1:$GWP/hubs \
  PROJECTX_USERNAME=dev PROJECTX_API_KEY=dev PX_ACCOUNT_ID=4242 PX_ACCOUNT_KIND=sim PX_BRACKET_MODE=position \
  PX_DAILY_LOSS_LIMIT=1000 PX_DRAWDOWN_BUFFER=2000 PX_NO_NEW_AFTER=15:45 PX_TRADING=1 CHART_PORT=$APPP \
  PX_STATE_PATH="$D/day.json" PX_INTENT_PATH="$D/intents.json" PX_AUDIT_PATH="$D/audit.jsonl" PX_HALT_FILE="$D/HALT"
"$PY" px_app.py > "$D/app.log" 2>&1 & APP=$!
echo "desk: http://127.0.0.1:$APPP/?ai=1   (mock gateway, SIM account, PX_TRADING=1 against the mock only)"
echo "logs: $D"
wait $APP
