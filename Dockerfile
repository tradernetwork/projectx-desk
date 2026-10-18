# ProjectX Desk. Binds 0.0.0.0 INSIDE the container; the desk refuses to start that way without PX_OPERATOR_TOKEN and
# PX_ALLOWED_HOSTS, and docker-compose.yml publishes the port on the host's loopback only. See docs/DEPLOY.md.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PX_HOST=0.0.0.0 \
    CHART_PORT=8600 \
    PX_STATE_PATH=/data/day_state.json \
    PX_INTENT_PATH=/data/intents.json \
    PX_AUDIT_PATH=/data/audit.jsonl \
    PX_HALT_FILE=/data/HALT \
    PX_LOCK_FILE=/data/desk.lock

RUN useradd --create-home --uid 10001 px && mkdir /data && chown px:px /data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
USER px

# The halt file, order count, intents and the one-desk lock live here. Losing this volume clears a halt and the day's counters.
VOLUME ["/data"]
EXPOSE 8600

HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('CHART_PORT','8600'), timeout=3)" || exit 1

CMD ["python", "-m", "projectx_desk"]
