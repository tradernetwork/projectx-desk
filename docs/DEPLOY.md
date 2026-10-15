# Running it in Docker, an LXC, or on another machine

By default the desk binds `127.0.0.1` and accepts only loopback Host headers. That is the safe default because the desk can place
orders. To run it anywhere else you must give it a login and an allowlist, or it **refuses to start**.

| Setting | Meaning |
|---|---|
| `PX_HOST` | the address to bind. `0.0.0.0` inside a container or LXC. Default `127.0.0.1`. |
| `PX_OPERATOR_TOKEN` | required for any non-loopback bind. 32+ characters, no placeholder words: `openssl rand -hex 32`. It is a login: HTTP Basic, any username, the token as the password. |
| `PX_ALLOWED_HOSTS` | required for any non-loopback bind. The names you will use to reach it, comma separated (`desk.lan,10.0.0.5`). Stops DNS-rebinding pages. Loopback is always allowed so health checks work. |
| `PX_ALLOWED_ORIGINS` | optional. Extra allowed `Origin` values, for example `https://desk.example.com` when a TLS proxy fronts it. Each allowed host is already accepted over http and https. |

The same rule applies when the app is launched some other way (for example `uvicorn` on `px_app`): `create_app()` refuses a public
bind without `PX_OPERATOR_TOKEN` and `PX_ALLOWED_HOSTS` too.

The login covers every route except `/mcp/` (which has its own bearer token, `PX_MCP_TOKEN`) and `/healthz` (which returns `ok` and
nothing else). **If you put a reverse proxy in front of a loopback-bound desk, set `PX_OPERATOR_TOKEN` anyway:** the refusal only
triggers on a non-loopback bind, and the proxy is what makes it reachable.

## The token is the whole gate

Anyone with the operator token can stage and place orders within your caps. HTTP Basic sends it in the clear, so **do not expose the desk
over plain HTTP beyond a network you fully control.** Use one of:

- **Tailscale:** `tailscale serve --bg 8600` gives you `https://<machine>.<tailnet>.ts.net`. Add that name to `PX_ALLOWED_HOSTS`.
- **A TLS reverse proxy** such as Caddy, which obtains a certificate for you:

  ```
  desk.example.com {
      reverse_proxy 127.0.0.1:8600
  }
  ```
  with `PX_ALLOWED_HOSTS=desk.example.com`. Server-sent events (the live ticks) pass through Caddy unchanged.

Do not forward the port from your router.

## Docker

```bash
cp .env.projectx.example .env.projectx     # fill it in; PX_OPERATOR_TOKEN and PX_ALLOWED_HOSTS are required here
docker compose up --build
```

The image binds `0.0.0.0` inside the container, runs as a non-root user with all capabilities dropped, and keeps its state (the halt
file, the day's order count, the order intents, the audit log) in the `px-data` volume. **Losing that volume clears a halt and the
day's counters.** `docker-compose.yml` publishes the port on the host's loopback only (`127.0.0.1:8600`). Changing that is a decision:
whatever address you publish on needs to be in `PX_ALLOWED_HOSTS`, and anything that leaves the host needs TLS.

The container files have not yet been run on a machine with Docker. CI builds the image and checks that it refuses to start without a
token; treat the first real run as a test. Once it runs, `read_only: true` plus a `tmpfs` for `/tmp` and `/home/px/.cache` is a
reasonable extra hardening step.

## LXC or a plain Linux box

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

A systemd unit (`/etc/systemd/system/projectx-desk.service`):

```ini
[Unit]
Description=ProjectX Desk
After=network-online.target

[Service]
User=px
WorkingDirectory=/opt/projectx-desk
EnvironmentFile=/etc/projectx-desk.env
ExecStart=/opt/projectx-desk/.venv/bin/python px_app.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/projectx-desk

[Install]
WantedBy=multi-user.target
```

`/etc/projectx-desk.env` (mode 0600, no inline comments, because systemd does not strip them):

```
PX_HOST=0.0.0.0
PX_OPERATOR_TOKEN=<openssl rand -hex 32>
PX_ALLOWED_HOSTS=desk.lan
PX_STATE_PATH=/var/lib/projectx-desk/day_state.json
PX_INTENT_PATH=/var/lib/projectx-desk/intents.json
PX_AUDIT_PATH=/var/lib/projectx-desk/audit.jsonl
PX_HALT_FILE=/var/lib/projectx-desk/HALT
PROJECTX_USERNAME=your-platform-login-name
PROJECTX_API_KEY=...
PX_ACCOUNT_ID=...
```

Login needs the **pair** `PROJECTX_USERNAME` + `PROJECTX_API_KEY`. `PROJECTX_USERNAME` is the username you sign in to the firm's
platform with (TopstepX): not your email, not an account name or id. `PX_FLATTEN_FALLBACK` defaults to `0`; leave it out unless you
have read [LIMITATIONS.md](LIMITATIONS.md) on what the fallback can do.

If the desk refuses to start, the reason is in `journalctl -u projectx-desk` and never contains the token.

## Check it

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<host>:8600/healthz                      # 200, no login
curl -s -o /dev/null -w "%{http_code}\n" http://<host>:8600/state.json                   # 401
curl -s -o /dev/null -w "%{http_code}\n" -u me:$PX_OPERATOR_TOKEN http://<host>:8600/state.json   # 200
```
