# Running it in Docker, an LXC, or on another machine

> **Topstep: where the desk may run.** Topstep's API terms say all trading must originate from your personal device, and that a
> VPS, VPN or remote server may read and log but **cannot place, modify, cancel or route orders**
> ([source and caveats](TOPSTEP_RULES.md)). A cloud server or any machine you do not physically control is the case that rule
> forbids. A Docker or LXC host on your own hardware at home is probably fine, but get Topstep support to confirm it in
> writing before you rely on it. For the safest reading, run the desk on the machine you trade from.

By default the desk binds `127.0.0.1` and accepts only loopback Host headers. That is the safe default because the desk can place
orders. To run it anywhere else you must give it a login and an allowlist, or it **refuses to start**.

| Setting | Meaning |
|---|---|
| `PX_HOST` | the address to bind. `0.0.0.0` inside a container or LXC. Default `127.0.0.1`. |
| `PX_OPERATOR_TOKEN` | required for any non-loopback bind. 32+ characters, no placeholder words: `openssl rand -hex 32`. It is a login: HTTP Basic, any username, the token as the password. |
| `PX_ALLOWED_HOSTS` | required for any non-loopback bind. The names you will use to reach it, comma separated (`desk.lan,10.0.0.5`). Stops DNS-rebinding pages. Loopback is always allowed so health checks work. |
| `PX_ALLOWED_ORIGINS` | optional. Extra allowed `Origin` values, for example `https://desk.example.com` when a TLS proxy fronts it. Each allowed host is already accepted over http and https. |

The same rule applies when the app is launched some other way (for example `uvicorn` on `projectx_desk.app`): `create_app()` refuses a public
bind without `PX_OPERATOR_TOKEN` and `PX_ALLOWED_HOSTS` too.

The login covers every route except `/mcp/` (which has its own bearer token, `PX_MCP_TOKEN`) and `/healthz` (which returns `ok` and
nothing else). **If you put a reverse proxy in front of a loopback-bound desk, set `PX_OPERATOR_TOKEN` anyway:** the refusal only
triggers on a non-loopback bind, and the proxy is what makes it reachable.

`PX_OPEN_LAN=1` is the explicit opt-out: a non-loopback bind with `PX_OPERATOR_TOKEN` left blank is then allowed (a weak token still
refuses, and `PX_ALLOWED_HOSTS` is still required). Only for a home network you fully control. Everything on that network can stage and
place within your caps, and the page is cleartext HTTP. Off by default.

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
file, the day's order count, the order intents, the audit log, the one-desk lock) in the `px-data` volume. **Losing that volume clears a halt and the
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
ExecStart=/opt/projectx-desk/.venv/bin/python -m projectx_desk
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
PX_LOCK_FILE=/var/lib/projectx-desk/desk.lock
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

## Keeping it running on your own Linux machine (systemd user service)

The desk records the trade tape for the buy/sell volume bars and the tick charts, and ProjectX has no tick history, so **every minute the
desk is not running is a hole you cannot backfill**. Run it as a user service so closing a terminal or an editor session does not stop
it. Save as `~/.config/systemd/user/projectx-desk.service` (adjust the paths), then
`systemctl --user daemon-reload && systemctl --user enable --now projectx-desk.service` (and `loginctl enable-linger $USER` to keep it
across logouts):

```
[Unit]
Description=ProjectX Desk
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/path/to/projectx-desk
ExecStart=/path/to/projectx-desk/.venv/bin/python -m projectx_desk
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=default.target
```

It keeps loading `.env.projectx` from the checkout root, and a clean stop saves the tape (`data/flow.json`). Logs:
`journalctl --user -u projectx-desk -f`. This is the same machine you trade from, so it stays inside Topstep's own-device rule.

## The recorder: a log-only copy for a server

Put this on a home server if you want buy/sell volume and tick bars to keep recording while your trading machine is off or restarting.
`PX_RECORD_ONLY=1` builds a desk with **no order path at all**, which is what Topstep's "a server may read and log" wording allows:

- It starts only the market-data hub, the trade-tape recording (volume, tick bars), the indicator tracker and its grade log.
- It builds no Guard, no account connection, no watchdog and no stop protection, mounts no MCP, and registers no POST route. Every desk
  write path returns 404. The routes are GET only: `/`, `/healthz`, `/bars`, `/api/flow`, `/api/tickbars`, `/api/tickbars/config`,
  `/api/indicators`, `/api/respect`.
- It refuses to start if `PX_TRADING` or `PX_MCP_PLACE` is set. It takes the one-desk lock like any desk, and the off-loopback rules
  (`PX_OPERATOR_TOKEN`, `PX_ALLOWED_HOSTS`) apply unchanged.
- Run it from its own checkout with its own `data/`. Never run a trading desk and a recorder from the same data directory.

`/etc/projectx-recorder.env` (mode 0600, no inline comments):

```
PX_RECORD_ONLY=1
PX_HOST=0.0.0.0
PX_OPERATOR_TOKEN=<openssl rand -hex 32>
PX_ALLOWED_HOSTS=recorder.lan
PX_TRACK_ROOTS=MES,MNQ,MGC
PX_FLOW_FILE=/var/lib/projectx-recorder/flow.json
PX_TICKS_FILE=/var/lib/projectx-recorder/ticks.json
PX_GRADES_FILE=/var/lib/projectx-recorder/indicator_grades.jsonl
PX_LOCK_FILE=/var/lib/projectx-recorder/desk.lock
PROJECTX_USERNAME=your-platform-login-name
PROJECTX_API_KEY=...
```

The unit is the one in the section above with `EnvironmentFile=/etc/projectx-recorder.env` and `ReadWritePaths=/var/lib/projectx-recorder`.

**Using the recorder from your trading desk.** Set `PX_RECORDER_URL` and `PX_RECORDER_TOKEN` (the recorder's operator token) on the trading desk.
The URL must be https, or http to loopback, so reach a LAN recorder through an ssh tunnel
(`ssh -N -L 8742:localhost:8600 recorder`, then `PX_RECORDER_URL=http://127.0.0.1:8742`) or `tailscale serve`. The desk fetches the
recorder's volume and tick bars every 60 seconds and fills only the holes in its own: a complete local minute always wins, nothing is
summed, nothing is interpolated, and a recorder that is down or sends garbage changes nothing. `/api/flow` and `/api/tickbars` then say
`source: merged` and report `recorder: ok | pending | unavailable (CODE)`. Set `PX_TRACK_ROOTS=0` on the trading desk if the recorder does the tracking.

## Phone alerts (installable app with Web Push)

The desk is an installable app and can push notifications to your phone. Browsers only allow push on HTTPS (or `localhost`), so on a LAN
you need a secure name in front of it. The simplest is Tailscale:

1. On the machine running the desk: `tailscale serve --bg 8600`. Add the name it prints (`<machine>.<tailnet>.ts.net`) to `PX_ALLOWED_HOSTS`.
   Keep the desk bound to loopback: the tailnet is then the gate and the operator login is not in the way.
2. On the phone, join the same tailnet and open that `https://` name. **iPhone:** iOS 16.4 or later only delivers web push to an app added to the
   Home Screen (Share, then Add to Home Screen), so install first and open it from the icon. **Android:** Chrome's install prompt or the menu works; push works even in a tab.
3. Tap **Alerts** in the top bar, then **Enable** (your tap is what lets the browser ask for permission), pick events, and press **Test**.

Defaults: ON for halt, flat, fill, stop hit or stop failure (and an unprotected position), loss or drawdown room under 25% (re-arms above 35%), and a feed down for more than 30 seconds.
OFF until you turn them on: a fresh analyst ENTRY callout and the regime flipping to RANGING. Halt, stop failure and unprotected position ignore quiet hours.
Notification text is a fixed short template (`Stop hit MES`): no account ids, balances, order tags or message text. `PX_PUSH_AMOUNTS=1` adds the net P&L of the closing fills to stop and flat texts (never a balance), which means it shows on a lock screen.
One notification per event per 60 seconds and 20 per hour overall.

| Setting | Meaning |
|---|---|
| `PX_VAPID_FILE`, `PX_PUSH_FILE` | the signing key and the subscriptions, both mode 0600 under `data/`. Keep them private; losing them only means re-enabling alerts on each device. |
| `PX_VAPID_SUB` | the contact the push services see, `mailto:you@example.com`. The default is a placeholder; set a real address if Apple rejects it. |
| `PX_PUSH_HOSTS` | extra push-service host suffixes. Subscriptions are only accepted for known push services (Google, Mozilla, Apple, Microsoft). |
| `PX_PUSH_AMOUNTS` | `1` adds dollars to stop and flat texts. Default off. |
| `PX_PUSH_GRACE_S`, `PX_PUSH_INTERVAL_S` | quiet period after boot (10) and how often state is checked (5, minimum 2). |

Alerts are a trading-desk feature. The recorder has no POST routes by design, so it has no alerts. If you do put the desk behind `PX_OPERATOR_TOKEN`
(off loopback), iOS Home Screen apps are known to handle HTTP Basic poorly and may re-prompt or fail to load the manifest: use the Tailscale route above instead.
Not verified on real hardware: delivery through Google or Apple, and iOS standalone behaviour.

