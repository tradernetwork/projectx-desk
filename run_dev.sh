#!/bin/bash
# Offline desk against the mock gateway: no credentials, no real orders. Ctrl+C stops both.
# Usage: ./run_dev.sh   then open http://127.0.0.1:8613/?ai=1
# Other ports (run two at once): CHART_PORT=8623 DEV_GATEWAY_PORT=8710 ./run_dev.sh
# Freeze the mock market (deterministic demos): DEV_GATEWAY_WALK=0 ./run_dev.sh
# Python: uses ./.venv if present, else python3. Override with PYTHON=/path/to/python.
# Needs bash and curl. Windows: run under WSL.
cd "$(dirname "$0")"
GWP=${DEV_GATEWAY_PORT:-8700}; APPP=${CHART_PORT:-8613}
if [ -n "$PYTHON" ]; then PY="$PYTHON"; elif [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
D=$(mktemp -d)
DEV_GATEWAY_PORT=$GWP "$PY" dev_gateway.py > "$D/gw.log" 2>&1 & GW=$!
trap 'kill $GW $APP 2>/dev/null' EXIT INT TERM
for _ in $(seq 1 60); do curl -fs "http://127.0.0.1:$GWP/dev/state" >/dev/null 2>&1 && break; sleep 0.25; done
# MCP: mounts only with a strong token. For the mock we make a throwaway one (kept in a 0600 file, not printed).
if [ -z "$PX_MCP_TOKEN" ]; then
  export PX_MCP_TOKEN=$("$PY" -c "import secrets; print(secrets.token_hex(32))")
  ( umask 077; printf '%s' "$PX_MCP_TOKEN" > "$D/mcp.token" )
fi
# The mock must never inherit the real desk's settings: no .env.projectx, no advisory feeds, its own tape file.
unset PX_FEED_URL PX_FEED_KEY PX_GEX_KEY PX_GEX_URL TD_API_KEY PX_MLL_FLOOR PX_FLAT_BY
export PX_NO_ENV_FILE=1 PX_FLOW_FILE="$D/flow.json"
export PROJECTX_API=http://127.0.0.1:$GWP PROJECTX_HUB_BASE=ws://127.0.0.1:$GWP/hubs \
  PROJECTX_USERNAME=dev PROJECTX_API_KEY=dev PX_ACCOUNT_ID=4242 PX_ACCOUNT_KIND=sim PX_BRACKET_MODE=position \
  PX_DAILY_LOSS_LIMIT=1000 PX_DRAWDOWN_BUFFER=2000 PX_NO_NEW_AFTER=16:55 PX_TRADING=1 CHART_PORT=$APPP \
  PX_STATE_PATH="$D/day.json" PX_INTENT_PATH="$D/intents.json" PX_AUDIT_PATH="$D/audit.jsonl" PX_HALT_FILE="$D/HALT"
"$PY" px_app.py > "$D/app.log" 2>&1 & APP=$!
for _ in $(seq 1 80); do curl -fs "http://127.0.0.1:$APPP/healthz" >/dev/null 2>&1 && break; kill -0 $APP 2>/dev/null || break; sleep 0.25; done
if ! curl -fs "http://127.0.0.1:$APPP/healthz" >/dev/null 2>&1; then
  echo "The desk did not start. Last lines of its log ($D/app.log):" >&2; tail -n 15 "$D/app.log" >&2; exit 1
fi
echo "desk: http://127.0.0.1:$APPP/?ai=1   (mock gateway, SIM account; PX_TRADING=1 applies to the mock only)"
echo "AI digest:  curl 'http://127.0.0.1:$APPP/ai/digest?contract=CON.F.US.MES.Z26&risk_pct=0.05'"
if [ -f "$D/mcp.token" ]; then
  echo "MCP:        claude mcp add --transport http projectx http://127.0.0.1:$APPP/mcp/ --header \"Authorization: Bearer \$(cat $D/mcp.token)\""
else
  echo "MCP:        mounted at http://127.0.0.1:$APPP/mcp/ only if your PX_MCP_TOKEN is strong (32+ characters, no placeholder words); see $D/app.log"
fi
echo "logs: $D"
wait $APP
