#!/usr/bin/env python
"""Compare two `rh_capture.py` files. Exit 1 when a tool the desk calls was removed or gained a required input.

    python scripts/rh_drift.py old.json new.json

The tools the desk calls are `projectx_desk.rh.mcp.READ_TOOLS`. Added tools and changes to tools the desk does not
call are printed but do not fail the run. A file may be a capture ({"tools": [...]}) or a bare tool list.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk.rh.mcp import READ_TOOLS  # noqa: E402


def load(path):
    d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    tools = d.get("tools") if isinstance(d, dict) else d
    if not isinstance(tools, list):
        raise ValueError("no tool list")
    out = {}
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            sch = t.get("input_schema") or t.get("inputSchema") or {}
            req = sch.get("required") if isinstance(sch, dict) else None
            out[t["name"]] = {str(x) for x in req} if isinstance(req, list) else set()
    return out


def drift(old, new, called=READ_TOOLS):
    """Returns (failures, notes)."""
    fail, notes = [], []
    for name in sorted(called):
        if name not in old:
            continue                                   # not in the baseline: nothing to compare
        if name not in new:
            fail.append(f"REMOVED {name}")
            continue
        added = sorted(new[name] - old[name])
        if added:
            fail.append(f"NEW REQUIRED INPUT {name}: {', '.join(added)}")
    for name in sorted(set(new) - set(old)):
        notes.append(f"added {name}")
    for name in sorted(set(old) - set(new) - set(called)):
        notes.append(f"removed (not called by the desk) {name}")
    return fail, notes


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        print("usage: rh_drift.py old.json new.json", file=sys.stderr)
        return 2
    try:
        old, new = load(argv[0]), load(argv[1])
    except (OSError, ValueError) as e:
        print(f"cannot read capture: {type(e).__name__}", file=sys.stderr)
        return 2
    fail, notes = drift(old, new)
    for n in notes:
        print(n)
    for f in fail:
        print(f)
    print("DRIFT" if fail else "no drift that affects the desk")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
