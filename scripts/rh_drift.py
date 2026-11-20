#!/usr/bin/env python
"""Compare two `rh_capture.py` files. Exit 1 when a tool the desk calls was removed or gained a required input.

    python scripts/rh_drift.py old.json new.json

The tools the desk calls are `projectx_desk.rh.mcp.READ_TOOLS`. Added tools and changes to tools the desk does not
call are printed but do not fail the run. Changed descriptions (compared by hash), property types and enums print as WARN lines and never
change the exit code. Read-only, offline, no secrets. A file may be a capture ({"tools": [...]}) or a bare tool list.
"""
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from projectx_desk.rh.mcp import READ_TOOLS  # noqa: E402


def _hash(text):
    return hashlib.sha256((text if isinstance(text, str) else "").encode("utf-8")).hexdigest()[:16]


def _props(sch):
    out = {}
    props = sch.get("properties") if isinstance(sch, dict) else None
    for k, v in (props.items() if isinstance(props, dict) else ()):
        if isinstance(v, dict):
            typ, en = v.get("type"), v.get("enum")
            out[str(k)] = {"type": json.dumps(typ, sort_keys=True) if typ is not None else None,
                           "enum": sorted(json.dumps(x, sort_keys=True) for x in en) if isinstance(en, list) else None}
    return out


def load(path):
    """name -> {"required": set, "desc": short sha256 of the description, "props": {name: {"type", "enum"}}}. Only shapes and a hash are kept."""
    d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    tools = d.get("tools") if isinstance(d, dict) else d
    if not isinstance(tools, list):
        raise ValueError("no tool list")
    out = {}
    for t in tools:
        if isinstance(t, dict) and isinstance(t.get("name"), str):
            sch = t.get("input_schema") or t.get("inputSchema") or {}
            req = sch.get("required") if isinstance(sch, dict) else None
            out[t["name"]] = {"required": {str(x) for x in req} if isinstance(req, list) else set(),
                              "desc": _hash(t.get("description")), "props": _props(sch)}
    return out


def drift(old, new, called=READ_TOOLS):
    """Returns (failures, warnings, notes). Failures (exit 1): a tool the desk calls vanished, or gained a required input. Warnings: a changed
    description hash, property type or enum on any tool both captures have. Notes: tools added or removed."""
    fail, warn, notes = [], [], []
    for name in sorted(called):
        if name not in old:
            continue                                   # not in the baseline: nothing to compare
        if name not in new:
            fail.append(f"REMOVED {name}")
            continue
        added = sorted(new[name]["required"] - old[name]["required"])
        if added:
            fail.append(f"NEW REQUIRED INPUT {name}: {', '.join(added)}")
    for name in sorted(set(old) & set(new)):
        o, n = old[name], new[name]
        tag = " (desk calls it)" if name in called else ""
        if o["desc"] != n["desc"]:
            warn.append(f"WARN description changed {name}{tag}: {o['desc']} -> {n['desc']}")
        for p in sorted(set(o["props"]) & set(n["props"])):
            a, b = o["props"][p], n["props"][p]
            if a["type"] != b["type"]:
                warn.append(f"WARN type changed {name}.{p}{tag}: {a['type']} -> {b['type']}")
            if a["enum"] != b["enum"]:
                gone, came = sorted(set(a["enum"] or ()) - set(b["enum"] or ())), sorted(set(b["enum"] or ()) - set(a["enum"] or ()))
                warn.append(f"WARN enum changed {name}.{p}{tag}: +{came} -{gone}")
        for p in sorted(set(n["props"]) - set(o["props"])):
            warn.append(f"WARN new input {name}.{p}{tag}")
        for p in sorted(set(o["props"]) - set(n["props"])):
            warn.append(f"WARN input removed {name}.{p}{tag}")
    for name in sorted(set(new) - set(old)):
        notes.append(f"added {name}")
    for name in sorted(set(old) - set(new) - set(called)):
        notes.append(f"removed (not called by the desk) {name}")
    return fail, warn, notes


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
    fail, warn, notes = drift(old, new)
    for n in notes:
        print(n)
    for w in warn:
        print(w)
    for f in fail:
        print(f)
    print("DRIFT" if fail else "no drift that affects the desk" + (f" ({len(warn)} warning(s))" if warn else ""))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
