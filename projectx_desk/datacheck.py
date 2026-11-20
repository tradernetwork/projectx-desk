"""Boot check for the data directory: refuse to start on a missing or unmounted one (called in app.main() BEFORE the lock).

Without it, an unmounted data pool is silently re-created empty on the root filesystem by the lock, the halt file it hid reads as
"not halted" and the daily order count restarts. Pure: reads the environment mapping and the filesystem, never creates, writes or
deletes anything, imports no guard, client, hub, mcp or app. The default `ROOT/data` is allowed to be absent (a fresh checkout has
none until the first run): its parent, the checkout, is tested instead. Messages name directories and flag names, never values.
Opt-in `PX_DATA_SENTINEL=1` also demands `data/.desk_data` (written by `python -m projectx_desk --init-data`).
"""
import pathlib

SENTINEL = ".desk_data"
INIT_CMD = "python -m projectx_desk --init-data"

# (environment name): the Limits paths and the lock file; each defaults to a file under ROOT/data.
_PATHS = ("PX_HALT_FILE", "PX_AUDIT_PATH", "PX_STATE_PATH", "PX_INTENT_PATH", "PX_LOCK_FILE")


def sentinel_on(env) -> bool:
    return str(env.get("PX_DATA_SENTINEL", "")).strip() == "1"


def _exists(p: pathlib.Path) -> bool:
    try:
        return p.exists()
    except OSError:
        return True            # a permission or junction oddity is not proof of a missing disk: never refuse on it


def _dangling(p: pathlib.Path) -> bool:
    try:
        return p.is_symlink() and not p.exists()
    except OSError:
        return False


def problem(env, root):
    """Return a refusal message, or None when the data directory looks present."""
    root = pathlib.Path(root)
    data = root / "data"
    if _dangling(data):
        return (f"the data directory {data} is a symlink to a missing target (an unmounted disk?). Refusing to start: a desk "
                f"here would lose its halt file and day counters. Mount it or fix the link, then start again.")
    for name in _PATHS:
        raw = str(env.get(name) or "").strip()
        parent = pathlib.Path(raw).parent if raw else data
        if _dangling(parent):
            return (f"{name}: the directory {parent} is a symlink to a missing target (an unmounted disk?). Refusing to start. "
                    f"Mount it or fix the link, then start again.")
        if parent == data:                       # default: a fresh checkout has no data/ yet, so test the checkout instead
            if not _exists(root):
                return f"the checkout {root} is missing (unmounted?). Refusing to start; mount it and try again."
            continue
        if not _exists(parent):
            return (f"{name} points into {parent}, which does not exist (an unmounted disk?). Refusing to start: it would be "
                    f"created empty and a halt file or the day's counters could be lost. Mount it, or create it on purpose "
                    f"with: mkdir -p {parent}")
    if sentinel_on(env) and not _exists(data / SENTINEL):
        return (f"PX_DATA_SENTINEL=1 and {data / SENTINEL} is missing: this is not the data directory the desk last ran on "
                f"(unmounted, or a new box). If it is the right one, run: {INIT_CMD}")
    return None


def init_data(root):
    """`--init-data`: create the data directory and its sentinel. Returns the sentinel path."""
    d = pathlib.Path(root) / "data"
    d.mkdir(parents=True, exist_ok=True)
    s = d / SENTINEL
    with open(s, "a", encoding="utf-8") as f:
        print("projectx-desk data directory", file=f)
    return s
