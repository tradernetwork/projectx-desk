#!/usr/bin/env python
"""Tamper evidence for the desk's append-only audit files. Standalone and read-only: it never imports the desk and never touches the
files it checks.

    python scripts/verify_audit.py --save                 # record the current head of each file (do this after a good session)
    python scripts/verify_audit.py                        # compare the files to the saved head
    python scripts/verify_audit.py --file data/audit.jsonl --head-file /safe/place/heads.json

Each file is a sha256 hash chain over its complete lines: h(0) = sha256(b""), h(i) = sha256(h(i-1) + line i). The saved head file (default
~/.px_audit_heads.json, mode 0600, kept OUTSIDE data/ so whatever can rewrite an audit file cannot also rewrite the baseline) holds the
line count, the head and one short hash per line, so a change can be located. A last line without a newline is still being written and is
not counted.

Verdicts per file: grew (new lines appended, OK), unchanged (OK), edited (an earlier line changed: the first line number that differs),
shrunk (lines removed or the file is gone), no baseline. Exit 0 all OK, 1 any file broken, 2 no baseline to compare with.
A file that is edited or shrunk is NOT re-saved over its baseline unless --force.

This detects an after-the-fact edit by anything that cannot also rewrite the head file. It does not stop one, and a process that can
write both places defeats it. It is not wired into the desk on purpose.
"""
import argparse
import hashlib
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_FILES = ("audit.jsonl", "rh_audit.jsonl", "trades.jsonl")
DEFAULT_HEAD = pathlib.Path.home() / ".px_audit_heads.json"
SHORT = 12                                    # hex chars kept per line in the head file


def chain(path):
    """(count, head_hex, [short hash per complete line]) of a file, or None when it cannot be read."""
    try:
        data = pathlib.Path(path).read_bytes()
    except OSError:
        return None
    h, shorts = hashlib.sha256(b"").digest(), []
    for line in data.split(b"\n")[:-1]:       # the piece after the last newline is a partial line: not counted
        h = hashlib.sha256(h + line).digest()
        shorts.append(h.hex()[:SHORT])
    return len(shorts), h.hex(), shorts


def verdict(saved, now):
    """(word, detail) from a saved entry ({"lines", "head", "hashes"}) and chain() output (None = file unreadable)."""
    n_saved, hashes = saved["lines"], saved["hashes"]
    if now is None:
        return "shrunk", "file is missing or unreadable"
    n_now, head, shorts = now
    for i in range(min(n_saved, n_now)):
        if shorts[i] != hashes[i]:
            return "edited", f"line {i + 1} differs from the baseline"
    if n_now < n_saved:
        return "shrunk", f"{n_saved} lines saved, {n_now} now"
    if n_now > n_saved:
        return "grew", f"{n_now - n_saved} line(s) appended"
    return "unchanged", f"{n_now} lines"


def load_heads(path):
    try:
        d = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _valid(e):
    return (isinstance(e, dict) and isinstance(e.get("lines"), int) and isinstance(e.get("head"), str)
            and isinstance(e.get("hashes"), list) and len(e["hashes"]) == e["lines"])


def save_heads(path, heads):
    """Atomic, mode 0600: a temp file in the same directory, then os.replace."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        print(json.dumps(heads, indent=1, sort_keys=True), file=f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def targets(files):
    if files:
        return [(str(pathlib.Path(f).expanduser().resolve()), True) for f in files]
    return [(str((ROOT / "data" / n).resolve()), False) for n in DEFAULT_FILES]      # (path, explicitly asked for)


def main(argv=None):
    ap = argparse.ArgumentParser(description="hash-chain check of the desk's audit files (read-only)")
    ap.add_argument("--file", action="append", help="audit file to check (repeatable); default data/audit.jsonl, rh_audit.jsonl, trades.jsonl")
    ap.add_argument("--head-file", default=str(DEFAULT_HEAD), help="where the saved heads live (keep it outside data/)")
    ap.add_argument("--save", action="store_true", help="record the current head of each file")
    ap.add_argument("--force", action="store_true", help="with --save: overwrite the baseline of a file that is edited or shrunk")
    a = ap.parse_args(argv)
    head_path = pathlib.Path(a.head_file).expanduser()
    heads = load_heads(head_path)
    broken = missing = checked = 0
    for path, asked in targets(a.file):
        now = chain(path)
        saved = heads.get(path) if _valid(heads.get(path)) else None
        if a.save:
            if now is None:
                print(f"{path}: not found, nothing saved" if asked else f"{path}: absent, skipped")
                continue
            if saved is not None and not a.force:
                word, detail = verdict(saved, now)
                if word in ("edited", "shrunk"):
                    print(f"{path}: {word} ({detail}); baseline NOT replaced (use --force after you have looked)")
                    broken += 1
                    continue
            heads[path] = {"lines": now[0], "head": now[1], "hashes": now[2]}
            checked += 1
            print(f"{path}: saved {now[0]} lines, head {now[1][:16]}")
            continue
        if saved is None:
            if now is None and not asked:
                continue                       # a default file this desk never made: nothing to say
            print(f"{path}: no baseline" + ("" if now is not None else " (and the file is missing)"))
            missing += 1
            continue
        word, detail = verdict(saved, now)
        checked += 1
        print(f"{path}: {word} ({detail})")
        if word in ("edited", "shrunk"):
            broken += 1
    if a.save:
        if checked:
            save_heads(head_path, heads)
        return 1 if broken else (0 if checked else 2)
    if broken:
        return 1
    return 2 if missing or not checked else 0


if __name__ == "__main__":
    sys.exit(main())
