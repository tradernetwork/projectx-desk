"""Which AI the desk's Ask box can use, and what is installed on this computer. Pure data, validation and presence-only detection.

Ported from the TraderMatrix Desktop provider layer (tradernetwork/tradermatrix-desktop, Apache-2.0: llm/provider.rs, cli_registry.rs,
url_guard.rs, llmProviders.ts) so the desk does not need a Claude subscription. Rules carried over:

* Detection is a PATH walk over ABSOLUTE entries (executable regular file, exact name, no shell) and runs NOTHING. The one function that
  executes a binary is `check_version`, called only on an explicit user click: 3 s timeout, environment cleared to PATH, empty temp cwd.
* A found CLI is usable only when it has a verified tool-less recipe (`claude`; `hermes` behind the experimental switch). Every other
  roster entry is detect-only: listed so the trader sees it was found, never run.
* Login is detect-only. This module never reads, copies or stores a subscription token or a credential file.
* A bearer key goes wherever the base URL points, so URLs are parsed (never prefix-matched): https anywhere, plain http only to loopback,
  no userinfo, query or fragment. A hosted preset ignores any stored URL; a local preset must stay on loopback.
* The AI surface stays an allowlist: this module never sees the desk state, only the provider table.

Nothing here talks to the guard, the client or the hubs; `tests/test_llm_providers.py` pins that.
"""
import ipaddress
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MAX_URL_LEN = 200
VERSION_TIMEOUT_S = 3.0
LOOPBACK_NAMES = {"localhost"}

# ---- CLI roster ---------------------------------------------------------------------------------------------------------------

USABLE, EXPERIMENTAL, DETECT_ONLY = "usable", "experimental", "unsupported"
NOTE = "The desk does not use it. It is listed only so you can see it was found."
NO_SWITCH = "It has no verified way to turn its tools off, so it could act on your computer."


@dataclass(frozen=True)
class CliEntry:
    id: str
    label: str
    binaries: tuple
    support: str = DETECT_ONLY
    provider: str = ""        # the provider id of its card (only claude and hermes have one)
    reason: str = ""          # one plain sentence: why it is not (fully) supported
    terms: str = NOTE         # the vendor-terms line shown on its card


def _e(id_, label, binaries, reason):
    return CliEntry(id_, label, tuple(binaries), DETECT_ONLY, "", reason, NOTE)


REGISTRY = (
    CliEntry("claude", "Claude", ("claude",), USABLE, "claude_cli", "",
             "Uses your installed Claude (your own login). Usage counts against your own Claude plan and is subject to Anthropic's terms."),
    CliEntry("codex", "Codex", ("codex",), DETECT_ONLY, "",
             "Even in its read-only mode it can still run commands, so it cannot be made text-only.",
             "OpenAI says automating a ChatGPT login is not meant for public or open-source apps."),
    CliEntry("gemini", "Gemini", ("gemini",), DETECT_ONLY, "",
             "It has no switch that turns all its tools off, and its terms do not allow other apps to use it.",
             "Google's terms do not allow third-party tools to use its backend."),
    CliEntry("agy", "Antigravity", ("agy",), DETECT_ONLY, "",
             "It only offers a read-only mode, not a text-only one, and its terms do not allow other apps to use it.",
             "Antigravity's terms do not allow third-party software to drive it."),
    CliEntry("opencode", "OpenCode", ("opencode",), DETECT_ONLY, "",
             "Your question would have to be typed on its command line, where other programs on this computer can read it, and it cannot turn its tools off.",
             NOTE),
    CliEntry("hermes", "Hermes agent", ("hermes",), EXPERIMENTAL, "hermes_cli",
             "Experimental and untested here: it stays off until you turn it on.",
             "Runs your own Hermes install under its own settings. You are responsible for what that install is allowed to do."),
    CliEntry("openclaw", "OpenClaw", ("openclaw", "clawdbot", "moltbot"), DETECT_ONLY, "",
             "It runs as a background gateway and takes your question on its command line, with no way to turn its tools off.", NOTE),
    _e("qwen", "Qwen Code", ("qwen",), NO_SWITCH),
    _e("goose", "Goose", ("goose",), "It is built to run tools on your computer and has no verified way to turn them off."),
    _e("cursor-agent", "Cursor Agent", ("cursor-agent",), NO_SWITCH),
    _e("kimi", "Kimi", ("kimi",), NO_SWITCH),
    _e("auggie", "Auggie", ("auggie",), NO_SWITCH),
    _e("copilot", "GitHub Copilot CLI", ("copilot",), NO_SWITCH),
    _e("droid", "Droid", ("droid",), NO_SWITCH),
    _e("kiro", "Kiro", ("kiro-cli", "kiro"), NO_SWITCH),
    _e("codebuddy", "CodeBuddy", ("codebuddy",), NO_SWITCH),
    _e("aider", "Aider", ("aider",), "It edits files in your project and has no text-only mode."),
    _e("amp", "Amp", ("amp",), NO_SWITCH),
    _e("crush", "Crush", ("crush",), NO_SWITCH),
    _e("cline", "Cline", ("cline",), NO_SWITCH),
    _e("qoder", "Qoder", ("qodercli", "qoder"), NO_SWITCH),
    _e("nanobot", "Nanobot", ("nanobot",), NO_SWITCH),
    _e("zeroclaw", "ZeroClaw", ("zeroclaw",), NO_SWITCH),
    _e("picoclaw", "PicoClaw", ("picoclaw",), NO_SWITCH),
    _e("nullclaw", "NullClaw", ("nullclaw",), NO_SWITCH),
)


def cli_entry(id_):
    return next((e for e in REGISTRY if e.id == id_), None)


def find_in_path(path_var, name):
    """First executable regular file named exactly `name` in an ABSOLUTE PATH entry (empty and relative entries skipped). Runs nothing."""
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    for d in (path_var or "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        f = os.path.join(d, name)
        try:
            if os.path.isfile(f) and os.access(f, os.X_OK):
                return f
        except OSError:
            continue
    return None


def scan(path_var=None):
    """Registry scan: which roster entries have an executable on the PATH. Only the binary NAME leaves this function, never the path."""
    pv = os.environ.get("PATH", "") if path_var is None else path_var
    out = []
    for e in REGISTRY:
        b = next((b for b in e.binaries if find_in_path(pv, b)), None)
        if b:
            out.append({"id": e.id, "label": e.label, "binary": b, "support": e.support, "reason": e.reason,
                        "terms": e.terms, "provider": e.provider or None})
    return out


def check_version(id_, path_var=None):
    """`<binary> --version` for one roster entry, only because the trader clicked. First line, control characters removed, at most 80 chars.
    Returns (text, None) or (None, code) with a fixed code: bad_request | not_installed | timeout | bad_reply."""
    e = cli_entry(id_)
    if e is None:
        return None, "bad_request"
    pv = os.environ.get("PATH", "") if path_var is None else path_var
    binp = next((p for p in (find_in_path(pv, b) for b in e.binaries) if p), None)
    if binp is None:
        return None, "not_installed"
    with tempfile.TemporaryDirectory(prefix="px-cli-") as cwd:
        try:
            r = subprocess.run([binp, "--version"], cwd=cwd, env={"PATH": pv}, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=VERSION_TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            return None, "timeout"
        except OSError:
            return None, "not_installed"
    if r.returncode != 0:
        return None, "bad_reply"
    line = next((ln.strip() for ln in r.stdout[:4096].decode("utf-8", "replace").splitlines() if ln.strip()), "")
    clean = "".join(ch for ch in line if ch.isprintable())[:80]
    return (clean, None) if clean else (None, "bad_reply")


@dataclass(frozen=True)
class Gate:
    """May a CLI provider id be saved and run? `claude_cli` always; `hermes_cli` only with the experimental switch; nothing else, ever."""
    experimental: bool = False

    def allows(self, provider):
        if provider == "claude_cli":
            return True
        if provider == "hermes_cli":
            return self.experimental
        return False


# ---- URL guard ----------------------------------------------------------------------------------------------------------------

def is_loopback_host(host):
    if not host:
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parts(raw):
    """The parsed URL, or None when it breaks the shared hygiene rules (userinfo, query, fragment, no host, odd scheme, too long)."""
    s = (raw or "").strip()
    if not s or len(s) > MAX_URL_LEN:
        return None
    try:
        u = urlsplit(s)
        host = u.hostname
        u.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not host or u.username is not None or u.password is not None or u.query or u.fragment or "@" in u.netloc:
        return None
    return u


def validate_base_url(raw):
    """Normalised URL (no trailing slash) or None. https anywhere; plain http only to a loopback host, judged on the parsed host."""
    u = _parts(raw)
    if u is None:
        return None
    if u.scheme == "http" and not is_loopback_host(u.hostname):
        return None
    return u._replace(path=u.path.rstrip("/")).geturl()


def is_private_lan(host):
    """RFC1918 (10/8, 172.16/12, 192.168/16) or the CGNAT block 100.64/10 (Tailscale). IPv4 literals only: not link-local, not IPv6, never a name."""
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host or ""):
        return False
    try:
        a = ipaddress.IPv4Address(host)
    except ValueError:
        return False
    o = a.packed
    return o[0] == 10 or (o[0] == 172 and 16 <= o[1] <= 31) or (o[0] == 192 and o[1] == 168) or (o[0] == 100 and 64 <= o[1] <= 127)


def validate_remote_agent_url(raw, lan_http=False):
    """For a self-hosted agent (Hermes): https to any host; plain http to loopback, or with `lan_http` to a private IPv4 literal. Names never qualify."""
    u = _parts(raw)
    if u is None:
        return None
    if u.scheme == "http" and not (is_loopback_host(u.hostname) or (lan_http and is_private_lan(u.hostname))):
        return None
    return u._replace(path=u.path.rstrip("/")).geturl()


def chat_url(base):
    """`<base>/chat/completions`; the base never ends in a slash after validation."""
    return base.rstrip("/") + "/chat/completions"


# ---- provider presets ---------------------------------------------------------------------------------------------------------

HTTP, CLI = "http", "cli"
PLAN = "price depends on your plan"


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    kind: str
    base_url: str
    key_help_url: str
    blurb: str
    profile: str              # request-body shape: openrouter | openai | venice | anthropic | local | custom | hermes | cli
    needs_key: bool
    default_model: str
    models: tuple = ()
    price_hint: str = PLAN

    @property
    def url_editable(self):
        return self.profile in ("local", "custom", "hermes")

    @property
    def takes_key(self):
        """The provider accepts a key at all (hosted presets, custom, hermes). Local presets and the CLIs never do."""
        return self.kind == HTTP and self.profile not in ("local",)

    @property
    def is_local(self):
        return self.profile == "local"


PRESETS = (
    Preset("openrouter", "OpenRouter", HTTP, "https://openrouter.ai/api/v1", "https://openrouter.ai/keys",
           "One key, many models, pay as you go. Cheap models cost cents.", "openrouter", True, "deepseek/deepseek-v4-flash",
           ("deepseek/deepseek-v4-flash", "deepseek/deepseek-v4.1-flash"),
           "from $0.18 per million output tokens (DeepSeek V4 Flash), a question is a fraction of a cent"),
    Preset("openai", "OpenAI", HTTP, "https://api.openai.com/v1", "https://platform.openai.com/api-keys",
           "ChatGPT's maker. Needs an API key with billing set up.", "openai", True, "gpt-4.1-mini", ("gpt-4.1-mini", "gpt-5-mini")),
    Preset("venice", "Venice", HTTP, "https://api.venice.ai/api/v1", "https://venice.ai/settings/api",
           "Privacy-focused hosted models.", "venice", True, "llama-3.3-70b", ("llama-3.3-70b",)),
    Preset("anthropic", "Anthropic", HTTP, "https://api.anthropic.com", "https://console.anthropic.com",
           "Use your own Anthropic API key.", "anthropic", True, "claude-haiku-5-5", ("claude-haiku-5-5", "claude-sonnet-5-5")),
    Preset("ollama", "Ollama (this computer)", HTTP, "http://127.0.0.1:11434/v1", "",
           "Runs a model on your own machine. Free, private, no key.", "local", False, "llama3.2", ("llama3.2", "qwen3"),
           "free, uses your computer"),
    Preset("lmstudio", "LM Studio (this computer)", HTTP, "http://127.0.0.1:1234/v1", "",
           "Runs a model on your own machine. Free, private, no key.", "local", False, "", (), "free, uses your computer"),
    Preset("custom", "Custom endpoint", HTTP, "", "", "Any OpenAI-compatible address you already use.", "custom", False, ""),
    Preset("hermes", "Hermes (self-hosted)", HTTP, "", "",
           "Your own Hermes agent, reached over HTTPS. You set it up and secure it.", "hermes", False, "",
           (), "runs on your own server, no price shown here"),
    Preset("claude_cli", "Claude (installed on this computer)", CLI, "", "",
           "Uses the Claude app you already signed in to. No key to paste.", "cli", False, ""),
    Preset("hermes_cli", "Hermes agent (installed on this computer)", CLI, "", "",
           "Experimental. Runs the Hermes you installed here, text only. Off unless you turn it on.", "cli", False, ""),
)


def preset(id_):
    return next((p for p in PRESETS if p.id == id_), None)


def effective_base_url(p, stored=None, lan_http=False):
    """The URL a config resolves to, validated, or None. Hosted presets ignore any stored value; local presets must stay on loopback;
    a self-hosted agent is normalised to end in /v1 (the preset talks to `<base>/chat/completions`)."""
    raw = (stored or "").strip() or p.base_url if p.url_editable else p.base_url
    if not raw:
        return None
    if p.profile == "hermes":
        u = validate_remote_agent_url(raw, lan_http)
        if u is None:
            return None
        return u if u.endswith("/v1") else u + "/v1"
    u = validate_base_url(raw)
    if u is None:
        return None
    if p.is_local and not is_loopback_host(urlsplit(u).hostname):
        return None
    return u


# ---- key shape ----------------------------------------------------------------------------------------------------------------

def key_shape_error(p, raw):
    """What is wrong with a pasted key, in plain words, or None. Catches the wrong-provider paste (sk-or- into OpenAI, etc.)."""
    k = (raw or "").strip()
    if not k:
        return f"Paste your {p.label} key first."
    if re.search(r"\s", k):
        return "The key must not contain spaces."
    if len(k) > 256:
        return "That key is too long."
    if p.id == "openrouter" and not (k.startswith("sk-or-") and len(k) > 6):
        return "OpenRouter keys start with sk-or-."
    if p.id == "anthropic" and not (k.startswith("sk-ant-") and len(k) > 7):
        return "Anthropic keys start with sk-ant-."
    if p.id == "openai" and (k.startswith("sk-or-") or k.startswith("sk-ant-")):
        return "That looks like a key for a different provider. Choose that provider's card instead."
    return None


MODEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,99}$")


def model_error(p, model):
    m = (model or "").strip()
    if m and not MODEL_OK.match(m):
        return "Model names use letters, digits and . _ / : @ + - only."
    if not m and not p.default_model:
        return "Type the model name (as your provider lists it)."
    return None


# ---- ordering and the payload the UI reads ------------------------------------------------------------------------------------

def probe_local_models(path_var=None):
    """Which local runtimes have a binary on PATH. Presence only; the server itself is not contacted here."""
    pv = os.environ.get("PATH", "") if path_var is None else path_var
    return {"ollama": bool(find_in_path(pv, "ollama")), "lmstudio": bool(find_in_path(pv, "lms"))}


def order_providers(found_cli, local, gate=None):
    """Detected providers first (stable table order inside each group), then the rest. `hermes_cli` exists only when found AND allowed."""
    gate = gate or Gate()
    cli_providers = {f["provider"] for f in found_cli if f["provider"]}
    cards = []
    for p in PRESETS:
        if p.id == "hermes_cli" and not (gate.allows("hermes_cli") and "hermes_cli" in cli_providers):
            continue
        found = (p.id in cli_providers) or bool(local.get(p.id))
        cards.append({"id": p.id, "label": p.label, "kind": p.kind, "blurb": p.blurb, "needs_key": p.needs_key, "takes_key": p.takes_key,
                      "url_editable": p.url_editable, "base_url": p.base_url, "key_help_url": p.key_help_url,
                      "default_model": p.default_model, "models": list(p.models), "price_hint": p.price_hint, "found": found})
    return [c for c in cards if c["found"]] + [c for c in cards if not c["found"]]


def detect(path_var=None, gate=None):
    """One read-only snapshot: ordered provider cards plus the found roster entries that have no usable card (shown as a plain note)."""
    gate = gate or Gate()
    found = scan(path_var)
    notes = [{"id": f["id"], "label": f["label"], "binary": f["binary"], "reason": f["reason"], "terms": f["terms"]}
             for f in found if f["support"] == DETECT_ONLY or (f["support"] == EXPERIMENTAL and not gate.allows(f["provider"] or ""))]
    return {"providers": order_providers(found, probe_local_models(path_var), gate), "found_cli": notes}
