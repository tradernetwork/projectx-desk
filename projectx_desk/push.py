"""Web Push sender: VAPID (RFC 8292, ES256) and aes128gcm payload encryption (RFC 8291), plus the key and subscription stores.

Pure send logic and files. No desk imports, no order path: the desk only ever PUSHES short fixed-template text to a browser's push
service. Built on `cryptography` and `httpx` (both already installed with the desk), so no new dependency.

Config (env, all optional):
  PX_VAPID_FILE   VAPID keypair, generated on first use (default data/vapid.json, mode 0600). The private key is never logged or served.
  PX_PUSH_FILE    subscriptions and their per-device toggles (default data/push_subs.json, mode 0600).
  PX_PUSH_HOSTS   extra comma-separated push-service host suffixes accepted for subscription endpoints (default: none).
  PX_VAPID_SUB    the `sub` claim, a mailto: or https: contact (default mailto:desk@example.com). Apple rejects a malformed one.

A subscription endpoint is attacker-influenced input (it arrives in a POST) and the desk will POST to it, so it is validated
against a push-service host allowlist: https only, no userinfo, no port other than 443, no IP literals.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import re
import secrets
import time
from urllib.parse import urlparse

import httpx
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    AVAILABLE = True        # `cryptography` arrives with fastmcp (via authlib); it is not in requirements.txt on its own
except ImportError:         # pragma: no cover - alerts switch off, the desk itself is unaffected
    AVAILABLE = False

PUSH_SUFFIXES = ("fcm.googleapis.com", "push.services.mozilla.com", "push.apple.com", "notify.windows.com")
MAX_ENDPOINT = 600
MAX_SUBS = 10
MAX_PAYLOAD = 2000          # RFC 8030 guarantees 4096 bytes after encryption; stay well under
RS = 4096
_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-]*[a-z0-9])?)+$")
_QUIET_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")

EVENT_KINDS = ("halt", "flat", "fill", "stop", "room", "feed", "callout", "regime")
EVENT_DEFAULTS = {"halt": True, "flat": True, "fill": True, "stop": True, "room": True, "feed": True, "callout": False, "regime": False}


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_\-]*", s):
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---- files ---------------------------------------------------------------------------------------------------------

def _atomic_private(path: pathlib.Path, text: str):
    """Write `text` to `path` through a 0600 temp file and an atomic rename. Never a bare `.write(` (the guard pin forbids it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    os.chmod(tmp, 0o600)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def default_paths(root: pathlib.Path, environ=None):
    env = os.environ if environ is None else environ
    return (pathlib.Path(env.get("PX_VAPID_FILE") or root / "data" / "vapid.json"),
            pathlib.Path(env.get("PX_PUSH_FILE") or root / "data" / "push_subs.json"))


# ---- VAPID (RFC 8292) ----------------------------------------------------------------------------------------------

class Vapid:
    def __init__(self, private_key: ec.EllipticCurvePrivateKey):
        self._key = private_key
        pub = private_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.public_b64 = b64u(pub)

    @classmethod
    def load_or_create(cls, path):
        path = pathlib.Path(path)
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            key = ec.derive_private_key(int.from_bytes(b64u_dec(d["private"]), "big"), ec.SECP256R1())
            v = cls(key)
            if d.get("public") == v.public_b64:
                return v
        except (OSError, ValueError, KeyError, TypeError):
            pass
        key = ec.generate_private_key(ec.SECP256R1())
        v = cls(key)
        raw = key.private_numbers().private_value.to_bytes(32, "big")
        _atomic_private(path, json.dumps({"private": b64u(raw), "public": v.public_b64}))
        return v

    def jwt(self, audience: str, sub: str, now: float | None = None, ttl: int = 12 * 3600) -> str:
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64u(json.dumps({"aud": audience, "exp": int((time.time() if now is None else now) + ttl), "sub": sub},
                                 separators=(",", ":")).encode())
        signing = f"{head}.{claims}".encode()
        r, s = decode_dss_signature(self._key.sign(signing, ec.ECDSA(hashes.SHA256())))
        return f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def authorization(self, endpoint: str, sub: str, now: float | None = None) -> str:
        u = urlparse(endpoint)
        return f"vapid t={self.jwt(f'{u.scheme}://{u.netloc}', sub, now)}, k={self.public_b64}"


def verify_jwt(token: str, public_b64: str) -> dict:
    """Test helper and sanity check: verify an ES256 VAPID JWT against a public key; returns the claims."""
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    head, claims, sig = token.split(".")
    raw = b64u_dec(sig)
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64u_dec(public_b64))
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    return json.loads(b64u_dec(claims))


# ---- payload encryption (RFC 8291, aes128gcm per RFC 8188) ---------------------------------------------------------

def encrypt(payload: bytes, p256dh: bytes, auth: bytes, as_private: ec.EllipticCurvePrivateKey | None = None,
            salt: bytes | None = None) -> bytes:
    """The request body for one push message. as_private and salt are injectable for the RFC test vector only."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
        raise ValueError("bad subscription keys")
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    as_key = as_private or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    salt = salt if salt is not None else secrets.token_bytes(16)
    if len(salt) != 16:
        raise ValueError("bad salt")
    ecdh = as_key.exchange(ec.ECDH(), ua_pub)
    prk_key = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + p256dh + as_pub).derive(ecdh)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(prk_key)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(prk_key)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)          # 0x02: last record, no padding
    return salt + RS.to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub + body


# ---- validation ----------------------------------------------------------------------------------------------------

def extra_suffixes(environ=None):
    raw = (os.environ if environ is None else environ).get("PX_PUSH_HOSTS") or ""
    hs = (h.strip().lower() for h in raw.split(","))
    return tuple(h for h in hs if _HOST_RE.match(h) and not re.fullmatch(r"[0-9.]+", h))      # never an IP literal


def endpoint_ok(url, extra=()) -> bool:
    """https only, a push-service host, no credentials, no odd port, no IP literal."""
    if not isinstance(url, str) or not url or len(url) > MAX_ENDPOINT or not url.isascii() or any(c in url for c in " \t\r\n\\"):
        return False
    try:
        u = urlparse(url)
        port = u.port
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or port not in (None, 443) or u.fragment or not host or not _HOST_RE.match(host):
        return False
    if host.replace(".", "").isdigit():                    # dotted-quad (the host regex already rejects most literals)
        return False
    return any(host == s or host.endswith("." + s) for s in PUSH_SUFFIXES + tuple(extra))


def clean_subscription(b, extra=()):
    """A browser PushSubscription.toJSON() -> {endpoint, p256dh, auth} or None. Key sizes are checked, the point must be on the curve."""
    if not isinstance(b, dict):
        return None
    keys = b.get("keys")
    ep = b.get("endpoint")
    if not isinstance(keys, dict) or not endpoint_ok(ep, extra):
        return None
    try:
        p, a = b64u_dec(keys.get("p256dh")), b64u_dec(keys.get("auth"))
        if len(p) != 65 or p[0] != 4 or len(a) != 16:
            return None
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p)
    except (ValueError, TypeError):
        return None
    return {"endpoint": ep, "p256dh": keys["p256dh"], "auth": keys["auth"]}


def clean_prefs(b, current=None):
    """Toggles and quiet hours from a request body: unknown keys dropped, non-bools ignored, quiet must be HH:MM-HH:MM or empty."""
    cur = current or {"events": dict(EVENT_DEFAULTS), "quiet": ""}
    ev = dict(EVENT_DEFAULTS) | dict(cur.get("events") or {})
    src = b.get("events") if isinstance(b, dict) and isinstance(b.get("events"), dict) else {}
    for k in EVENT_KINDS:
        if isinstance(src.get(k), bool):
            ev[k] = src[k]
    quiet = cur.get("quiet", "")
    if isinstance(b, dict) and "quiet" in b:
        q = b["quiet"]
        quiet = q if isinstance(q, str) and (q == "" or _QUIET_RE.match(q)) else quiet
    return {"events": {k: bool(ev[k]) for k in EVENT_KINDS}, "quiet": quiet}


def in_quiet(quiet: str, minutes_now: int) -> bool:
    """Local minutes since midnight inside HH:MM-HH:MM (wraps midnight). Empty or malformed = never quiet."""
    if not isinstance(quiet, str) or not _QUIET_RE.match(quiet):
        return False
    a, b = quiet.split("-")
    s, e = int(a[:2]) * 60 + int(a[3:]), int(b[:2]) * 60 + int(b[3:])
    if s == e:
        return False
    return s <= minutes_now < e if s < e else (minutes_now >= s or minutes_now < e)


# ---- subscription store --------------------------------------------------------------------------------------------

def sub_id(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


class PushStore:
    def __init__(self, path):
        self.path = pathlib.Path(path)
        self.subs = {}
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            for k, v in (d.get("subs") or {}).items():
                if _ID_RE.match(k) and isinstance(v, dict) and {"endpoint", "p256dh", "auth"} <= set(v):
                    self.subs[k] = {"endpoint": v["endpoint"], "p256dh": v["p256dh"], "auth": v["auth"],
                                    "prefs": clean_prefs({}, v.get("prefs")), "created": v.get("created", 0)}
        except (OSError, ValueError, AttributeError, TypeError):
            self.subs = {}

    def save(self):
        _atomic_private(self.path, json.dumps({"v": 1, "subs": self.subs}))

    def add(self, sub: dict, prefs=None):
        sid = sub_id(sub["endpoint"])
        if sid not in self.subs and len(self.subs) >= MAX_SUBS:
            raise ValueError("too many subscriptions")
        old = self.subs.get(sid)
        self.subs[sid] = {**sub, "prefs": clean_prefs(prefs or {}, (old or {}).get("prefs")), "created": (old or {}).get("created") or int(time.time())}
        self.save()
        return sid

    def remove(self, sid):
        if self.subs.pop(sid, None) is not None:
            self.save()
            return True
        return False

    def prefs(self, sid, body):
        s = self.subs.get(sid)
        if s is None:
            return None
        s["prefs"] = clean_prefs(body, s["prefs"])
        self.save()
        return s["prefs"]


# ---- sending -------------------------------------------------------------------------------------------------------

async def send(sub: dict, payload: dict, vapid: Vapid, sub_claim: str, client: httpx.AsyncClient | None = None, ttl: int = 600) -> str:
    """One push. Returns 'ok', 'gone' (404/410: delete the subscription), 'retry' (429/5xx/network) or 'fail' (anything else). Never raises."""
    try:
        body = encrypt(json.dumps(payload, separators=(",", ":")).encode(), b64u_dec(sub["p256dh"]), b64u_dec(sub["auth"]))
        headers = {"Authorization": vapid.authorization(sub["endpoint"], sub_claim), "TTL": str(ttl), "Urgency": "high",
                   "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream"}
    except (ValueError, KeyError, TypeError):
        return "fail"
    own = client is None
    c = client or httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    try:
        r = await c.post(sub["endpoint"], content=body, headers=headers)
    except httpx.HTTPError:
        return "retry"
    finally:
        if own:
            await c.aclose()
    if r.status_code in (200, 201, 202):
        return "ok"
    if r.status_code in (404, 410):
        return "gone"
    if r.status_code == 429 or r.status_code >= 500:
        return "retry"
    return "fail"


class Pusher:
    """Vapid + store + sender. `broadcast` fans one message to every subscription that has that event kind on, pruning dead ones.
    The VAPID keypair is created on first use (the first key request or the first send), not at desk start."""

    def __init__(self, vapid, store: PushStore, sub_claim: str = "mailto:desk@example.com", client=None, clock=time.localtime,
                 vapid_path=None):
        self._vapid, self.vapid_path = vapid, vapid_path
        self.store, self.sub_claim, self.client, self.clock = store, sub_claim, client, clock

    @property
    def vapid(self) -> Vapid:
        if self._vapid is None:
            self._vapid = Vapid.load_or_create(self.vapid_path)
        return self._vapid

    @classmethod
    def from_env(cls, root, client=None, environ=None):
        env = os.environ if environ is None else environ
        vp, pp = default_paths(root, env)
        sub = (env.get("PX_VAPID_SUB") or "").strip()
        sub = sub if re.match(r"^(mailto:[^\s@]+@[^\s@]+|https://[^\s]+)$", sub) and len(sub) <= 200 else "mailto:desk@example.com"
        return cls(None, PushStore(pp), sub, client, vapid_path=vp)

    def minutes_now(self):
        t = self.clock()
        return t.tm_hour * 60 + t.tm_min

    async def send_to(self, sid, payload) -> str:
        s = self.store.subs.get(sid)
        if s is None:
            return "gone"
        res = await send(s, payload, self.vapid, self.sub_claim, self.client)
        if res == "gone":
            self.store.remove(sid)
        return res

    async def broadcast(self, kind, payload, critical=False) -> int:
        n = 0
        for sid, s in list(self.store.subs.items()):
            p = s["prefs"]
            if not p["events"].get(kind) or (not critical and in_quiet(p["quiet"], self.minutes_now())):
                continue
            if await self.send_to(sid, payload) == "ok":
                n += 1
        return n
