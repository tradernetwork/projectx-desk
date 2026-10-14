"""Network exposure rules for the desk. Pure functions, no I/O.

Default: bind 127.0.0.1 and accept only loopback Host headers. To bind anywhere else (a container, an LXC, another
machine) the desk REFUSES TO START unless it has a strong operator token and an explicit host allowlist, because the
desk can place orders and, on loopback, relies on nothing else being able to reach it.

The operator token is a login: HTTP Basic, any username, the token as the password. It protects every route except
/mcp (which has its own bearer token) and /healthz (which returns the word "ok" and nothing else).
Basic auth over plain HTTP sends the token in the clear, so put TLS in front of any non-loopback bind (Tailscale, or a
reverse proxy such as Caddy). See docs/DEPLOY.md.
"""
import base64
import hmac
import ipaddress
from typing import Optional

LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1", "[::1]"}
PUBLIC_PATHS = ("/healthz",)          # exact paths that skip the operator login
OWN_AUTH_PREFIXES = ("/mcp",)         # paths with their own bearer-token auth


def is_loopback(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        return False


def _split(raw: Optional[str]):
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


def parse_hosts(raw: Optional[str], port: int) -> set:
    """'desk.lan, 10.0.0.5:8600' -> {desk.lan, desk.lan:8600, 10.0.0.5:8600}. A Host header may or may not carry the port."""
    out = set()
    for h in _split(raw):
        out.add(h)
        if ":" not in h.replace("[", "").replace("]", "") or h.endswith("]"):
            out.add(f"{h}:{port}")
    return out


def allowed_hosts(port: int, extra_raw: Optional[str] = None) -> set:
    """Loopback hosts are ALWAYS allowed (health checks, local use); extras come from PX_ALLOWED_HOSTS."""
    base = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    return base | parse_hosts(extra_raw, port)


def allowed_origins(hosts: set, extra_raw: Optional[str] = None) -> set:
    """Every allowed host over http and https, plus any explicit PX_ALLOWED_ORIGINS (e.g. a TLS reverse proxy)."""
    out = {f"http://{h}" for h in hosts} | {f"https://{h}" for h in hosts}
    return out | set(_split(extra_raw))


def bind_problem(host: str, operator_token: Optional[str], extra_hosts_raw: Optional[str], weak_reason=None) -> Optional[str]:
    """Why the desk must not start on this bind, or None. Never quotes the token."""
    if is_loopback(host):
        return None
    if not _split(extra_hosts_raw):
        return (f"refusing to bind {host}: set PX_ALLOWED_HOSTS to the name(s) you will use to reach the desk "
                f"(for example desk.lan,10.0.0.5). Without it a DNS-rebinding page could talk to the desk.")
    why = weak_reason(operator_token) if weak_reason else (None if operator_token else "no token set")
    if why:
        return (f"refusing to bind {host}: PX_OPERATOR_TOKEN is not usable ({why}). The desk can place orders; anything that can "
                f"reach it needs a login. Generate one with: openssl rand -hex 32")
    return None


def operator_ok(authorization: Optional[str], token: Optional[str]) -> bool:
    """HTTP Basic: any username, the operator token as the password. Constant-time compare."""
    if not token or not authorization or not authorization.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(authorization[6:].strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False
    _, _, password = raw.partition(":")
    return hmac.compare_digest(password.encode(), token.encode())


def needs_operator_login(path: str) -> bool:
    return path not in PUBLIC_PATHS and not any(path == p or path.startswith(p + "/") for p in OWN_AUTH_PREFIXES)
