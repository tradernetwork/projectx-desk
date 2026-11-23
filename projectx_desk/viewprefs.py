"""Display preferences the desk agent may change (Round 38). Pure plus one small file: no guard, client, hub, mcp or app import.

A fixed allowlist of chart toggles the UI already keeps as `px.*` flags. Every value is a boolean. Nothing here names an order, a
ticket, the halt, sizing or a limit, and nothing here is read by the guard: a preference only says what the page draws.
`set_view` in `mcp.py` is the only writer; `/state.json` carries `view` (read only) and `static/viewprefs.js` applies it.
Refusals are fixed words, never the caller's text.
"""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# key -> the UI flag it mirrors. Booleans only. Adding a key is a decision (docs/DECISIONS.md Round 38), not a drive-by.
KEYS = {
    "gex": "px.gex",              # GEX lines on the chart
    "chartMore": "px.chartMore",  # the More fold (far walls, other-timeframe lines, auto levels, ORs, reaction markers)
    "tmap": "px.tmap",            # tomorrow's-map overlay
    "trend": "px.trend",          # automatic trendlines
    "trend_1m": "px.trendTfs.1m",   # trendlines found on the 1m, drawn on the chart on screen
    "trend_5m": "px.trendTfs.5m",
    "trend_15m": "px.trendTfs.15m",
}
CODES = ("ok", "unknown_key", "bad_value", "unavailable")
_TRUE, _FALSE = ("true", "on", "yes", "1"), ("false", "off", "no", "0")


def parse_value(value):
    """True, False, or None (not a boolean). Accepts a real bool or the words true/false/on/off, nothing else."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return True
        if v in _FALSE:
            return False
    return None


def path_from_env(environ=None):
    e = os.environ if environ is None else environ
    p = (e.get("PX_VIEW_FILE") or "").strip()
    return Path(p) if p else ROOT / "data" / "view_prefs.json"


class ViewPrefs:
    """In-memory prefs (only keys somebody set), persisted to a small JSON file. A garbled or unwritable file never breaks the desk."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.prefs = {}
        self._load()

    @classmethod
    def from_env(cls, environ=None):
        return cls(path_from_env(environ))

    def _load(self):
        if self.path is None:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict):
            self.prefs = {k: v for k, v in raw.items() if k in KEYS and isinstance(v, bool)}

    def _save(self):
        if self.path is None:
            return
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                print(json.dumps(self.prefs, sort_keys=True), file=f)
            os.replace(tmp, self.path)
        except OSError:
            pass                      # the change still holds in memory; a restart forgets it

    def set(self, key, value):
        """(ok, code). Unknown key and non-boolean value are refused with a fixed code; nothing changes."""
        if not isinstance(key, str) or key not in KEYS:
            return False, "unknown_key"
        b = parse_value(value)
        if b is None:
            return False, "bad_value"
        self.prefs[key] = b
        self._save()
        return True, "ok"

    def view(self):
        """The read-only block for /state.json: only keys that were set, in key order. The UI leaves every other toggle to the trader."""
        return {"prefs": {k: self.prefs[k] for k in KEYS if k in self.prefs}, "keys": list(KEYS)}
