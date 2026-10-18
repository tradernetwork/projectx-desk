"""One desk per checkout: an exclusive process lock taken at boot.

Two desks on one account would each run the watchdog, the stop owner and the halt file, and could place the same trade twice.
The lock is an OS advisory lock on a file under data/ (PX_LOCK_FILE), released by the kernel when the process dies, so a crash
never leaves a stale lock behind. Taken in main() only: tests and `create_app()` callers are not affected.
"""
import os
import pathlib

from projectx_desk import ROOT

try:
    import fcntl
except ImportError:                      # Windows
    fcntl = None
    import msvcrt


def lock_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("PX_LOCK_FILE") or ROOT / "data" / "desk.lock")


def acquire(path=None):
    """Return the open lock file (keep a reference for the life of the process). Raises SystemExit if another desk holds it."""
    p = pathlib.Path(path) if path else lock_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        f = open(p, "a+")
    except OSError as e:                 # cannot prove we are the only desk: refuse, never guess
        raise SystemExit(f"cannot open the desk lock file {p} ({type(e).__name__}); refusing to start")
    try:
        if fcntl is not None:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        f.close()
        raise SystemExit(f"another projectx-desk already holds {p}. Two desks on one account can place the same trade twice; "
                         "stop the other one first.")
    try:
        f.seek(0)
        f.truncate()
        print(os.getpid(), file=f, flush=True)     # the pid is a hint for a human, not the lock
    except OSError:
        pass
    return f
