"""The Ask box's saved AI choice: which provider, which model, and (for a hosted or custom endpoint) the key. Persisted in
`data/llm_config.json` (`PX_LLM_CONFIG`), mode 0600, written atomically.

Rules (ported from the TraderMatrix Desktop config layer, Apache-2.0, adapted to a file instead of an OS keychain):

* The key is SEALED to the provider and the effective URL it was supplied for. Change either and the key is dropped: it must be entered
  again (`needs_key_again`), because a bearer key goes wherever the URL points.
* No getter returns the key. `view()` says `key_present` and nothing more. The one internal read is `resolve()`, whose result hides the
  key from `repr` and is meant only for `llm_chat`.
* A hosted preset ignores any stored URL (`llm_providers.effective_base_url`). A local preset stays on loopback.
* An unreadable or garbled file is "unconfigured", never a guess.
* `PX_LLM_KEY` (headless boxes) is read at use time, never persisted, and used ONLY for a hosted preset (fixed URL), so it can never be
  sent to a custom address.
* Errors are fixed codes. Messages come from `llm_providers` (they never contain the key).

This module never talks to the guard, the client or the hubs.
"""
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import llm_providers as lp

ROOT = Path(__file__).resolve().parent.parent
MAX_FILE = 16_384


class ConfigError(Exception):
    """`code` is a fixed word; `message` is plain words for the UI and never carries a key."""

    def __init__(self, code, message=""):
        super().__init__(code)
        self.code, self.message = code, message or code


def config_path(environ=None):
    env = os.environ if environ is None else environ
    raw = (env.get("PX_LLM_CONFIG") or "").strip()
    return Path(raw) if raw else ROOT / "data" / "llm_config.json"


def gate(environ=None):
    env = os.environ if environ is None else environ
    return lp.Gate(experimental=(env.get("PX_LLM_EXPERIMENTAL") or "").strip() == "1")


@dataclass(frozen=True)
class Stored:
    provider: str
    base_url: str = ""
    model: str = ""
    lan_http: bool = False
    key: str = field(default="", repr=False, compare=False)
    key_seal: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class Resolved:
    """What `llm_chat` needs. The key is hidden from repr and str."""
    preset: object
    base_url: str
    model: str
    key: str = field(default="", repr=False, compare=False)


def _seal(provider, url):
    return f"{provider}|{url}"


def _read(path):
    try:
        p = Path(path)
        if p.stat().st_size > MAX_FILE:
            return None
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    prov = d.get("provider")
    if not isinstance(prov, str) or lp.preset(prov) is None:
        return None
    s = {}
    for k in ("base_url", "model", "key", "key_seal"):
        v = d.get(k, "")
        if not isinstance(v, str):
            return None
        s[k] = v
    lan = d.get("lan_http", False)
    if not isinstance(lan, bool):
        return None
    return Stored(prov, s["base_url"], s["model"], lan, s["key"], s["key_seal"])


def load(path=None, environ=None):
    """The stored choice or None (missing, unreadable, garbled, or a CLI id the gate does not allow)."""
    st = _read(path or config_path(environ))
    if st is None:
        return None
    p = lp.preset(st.provider)
    if p.kind == lp.CLI and not gate(environ).allows(p.id):
        return None
    return st


def _eff_url(p, st):
    return lp.effective_base_url(p, st.base_url, st.lan_http) if p.kind == lp.HTTP else None


def _key_usable(p, st):
    """Stored key present AND sealed to the provider and URL it would be sent to now."""
    if not st.key or p.kind != lp.HTTP or not p.takes_key:
        return False
    url = _eff_url(p, st)
    return bool(url) and st.key_seal == _seal(p.id, url)


def _env_key(p, environ):
    env = os.environ if environ is None else environ
    k = (env.get("PX_LLM_KEY") or "").strip()
    if not k or p.kind != lp.HTTP or not p.takes_key or p.url_editable or lp.key_shape_error(p, k):
        return ""
    return k


def view(path=None, environ=None):
    """What the UI may read. No key, ever."""
    st = load(path, environ)
    if st is None:
        return {"configured": False, "provider": "", "base_url": "", "model": "", "lan_http": False,
                "key_present": False, "key_from_env": False, "needs_key_again": False, "url_ok": False}
    p = lp.preset(st.provider)
    url = _eff_url(p, st)
    stored_ok = _key_usable(p, st)
    env_ok = bool(_env_key(p, environ))
    return {"configured": True, "provider": p.id, "kind": p.kind, "base_url": url or "", "model": st.model or p.default_model,
            "lan_http": st.lan_http, "key_present": stored_ok or env_ok, "key_from_env": env_ok and not stored_ok,
            "needs_key_again": bool(st.key) and not stored_ok and p.takes_key, "url_ok": p.kind != lp.HTTP or bool(url)}


def route(path=None, environ=None):
    """'http' when a configured HTTP provider should answer the Ask box; 'cli' (use the existing deskagent relay) otherwise."""
    st = load(path, environ)
    if st is None:
        return "cli"
    p = lp.preset(st.provider)
    return "http" if p.kind == lp.HTTP else "cli"


def save(update, path=None, environ=None):
    """Apply `update` (any of provider, base_url, model, lan_http, key) over the stored choice and persist. Returns `view()`.
    Raises ConfigError with a fixed code. Changing the provider or the effective URL without a new key drops the old key."""
    if not isinstance(update, dict):
        raise ConfigError("bad_request")
    path = path or config_path(environ)
    old = load(path, environ)
    prov = update.get("provider", old.provider if old else None)
    p = lp.preset(prov) if isinstance(prov, str) else None
    if p is None:
        raise ConfigError("bad_provider", "Choose a provider from the list.")
    if p.kind == lp.CLI:
        if not gate(environ).allows(p.id):
            raise ConfigError("provider_off", "That provider is not available.")
        new = Stored(p.id)
    else:
        same = old is not None and old.provider == p.id
        base = update.get("base_url", old.base_url if same else "") if p.url_editable else ""
        model = update.get("model", old.model if same else "")
        lan = update.get("lan_http", old.lan_http if same else False)
        if not (isinstance(base, str) and isinstance(model, str) and isinstance(lan, bool)):
            raise ConfigError("bad_request")
        base, model = base.strip(), model.strip()
        if lan and p.profile != "hermes":
            lan = False
        cand = Stored(p.id, base, model, lan)
        url = _eff_url(p, cand)
        if not url:
            raise ConfigError("bad_url", "That address is not allowed. Use https, or http only to this computer.")
        msg = lp.model_error(p, model)
        if msg:
            raise ConfigError("bad_model", msg)
        key, seal = "", ""
        new_key = update.get("key")
        if new_key is not None and new_key != "":
            if not isinstance(new_key, str):
                raise ConfigError("bad_request")
            if not p.takes_key:
                raise ConfigError("no_key_needed", "That provider does not take a key.")
            msg = lp.key_shape_error(p, new_key)
            if msg:
                raise ConfigError("bad_key", msg)
            key, seal = new_key.strip(), _seal(p.id, url)
        elif old is not None and _key_usable(p, old) and old.key_seal == _seal(p.id, url) and update.get("clear_key") is not True:
            key, seal = old.key, old.key_seal       # same provider, same URL: the sealed key stays
        new = Stored(p.id, base, model, lan, key, seal)
    _write(path, new)
    out = view(path, environ)
    out["needs_key_again"] = out["needs_key_again"] or bool(old and old.key and p.takes_key and not new.key and p.kind == lp.HTTP)
    return out


def _write(path, st):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"provider": st.provider, "base_url": st.base_url, "model": st.model, "lan_http": st.lan_http,
                       "key": st.key, "key_seal": st.key_seal}, indent=1)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".llm_config.", suffix=".tmp")     # mkstemp creates the file 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            print(body, file=f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ConfigError("write_failed", "Could not save the setting.")


def clear(path=None, environ=None):
    """Forget the choice and the key."""
    try:
        Path(path or config_path(environ)).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        raise ConfigError("write_failed", "Could not clear the setting.")
    return view(path, environ)


def resolve(path=None, environ=None):
    """Internal, for `llm_chat` only. A `Resolved` for an HTTP provider (key possibly empty), or None when unconfigured or CLI."""
    st = load(path, environ)
    if st is None:
        return None
    p = lp.preset(st.provider)
    if p.kind != lp.HTTP:
        return None
    url = _eff_url(p, st)
    if not url:
        return None
    key = st.key if _key_usable(p, st) else _env_key(p, environ)
    return Resolved(p, url, st.model or p.default_model, key)
