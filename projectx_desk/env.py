"""Tiny .env loader so `python px_app.py` honours `.env.projectx` (no dependency, never logs values).

Rules: KEY=VALUE lines, optional `export `, `#` comments (a `#` after whitespace in an unquoted value starts a
trailing comment), optional single or double quotes. Variables already in the environment WIN, so a shell
`export` or a systemd unit always overrides the file. Returns the key NAMES it set, never the values.
"""
import os
import pathlib
import re

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_text(text: str) -> dict:
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if not _KEY.match(key):
            continue
        if val[:1] in ("'", '"') and val.find(val[0], 1) > 0:
            val = val[1:val.find(val[0], 1)]
        else:
            val = "" if val.startswith("#") else re.split(r"\s+#", val, maxsplit=1)[0].strip()
        out[key] = val
    return out


def load_env_file(path=None, environ=None) -> list:
    """Load PX_ENV_FILE, or ./.env.projectx, or the file next to this module. First that exists wins."""
    env = os.environ if environ is None else environ
    if env.get("PX_NO_ENV_FILE") == "1":        # offline/mock runs must never pick up the real account's settings
        return []
    candidates = [path] if path else [env.get("PX_ENV_FILE"), pathlib.Path.cwd() / ".env.projectx",
                                      pathlib.Path(__file__).resolve().parent.parent / ".env.projectx"]
    for c in candidates:
        if c and pathlib.Path(c).is_file():
            vals = parse_env_text(pathlib.Path(c).read_text(encoding="utf-8-sig"))
            set_keys = []
            for k, v in vals.items():
                if k not in env and v != "":
                    env[k] = v
                    set_keys.append(k)
            return set_keys
    return []
