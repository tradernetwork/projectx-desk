#!/usr/bin/env python3
"""Report raw tape log bytes per day per root from existing files (projectx_desk/tapelog.py), to size it before enabling all roots.

    python scripts/tape_size.py [DIR]      # default: PX_TAPE_DIR or data/tape under the checkout
Read-only. Works on any directory of `<ROOT>_<YYYY-MM-DD>.tape` files.
"""
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

FILE_RE = re.compile(r"^([A-Z0-9]{1,8})_(\d{4}-\d{2}-\d{2})\.tape$")


def scan(directory):
    """{root: {day: (bytes, lines)}}"""
    out = defaultdict(dict)
    for name in sorted(os.listdir(directory)):
        m = FILE_RE.match(name)
        if not m:
            continue
        p = os.path.join(directory, name)
        with open(p, "rb") as f:
            n = sum(1 for _ in f)
        out[m.group(1)][m.group(2)] = (os.path.getsize(p), n)
    return out


def report(directory):
    data = scan(directory)
    if not data:
        return "no tape files in " + str(directory)
    rows = ["root  day            bytes      MB  lines"]
    for root in sorted(data):
        days = data[root]
        for day in sorted(days):
            b, n = days[day]
            rows.append(f"{root:<5} {day}  {b:>10}  {b / 1048576:>6.2f}  {n}")
        avg = sum(b for b, _ in days.values()) / len(days)
        rows.append(f"{root:<5} average over {len(days)} day(s): {avg / 1048576:.2f} MB/day")
    total = sum(sum(b for b, _ in d.values()) / len(d) for d in data.values())
    rows.append(f"all roots shown: {total / 1048576:.2f} MB/day")
    return "\n".join(rows)


def main(argv):
    root = Path(__file__).resolve().parent.parent
    d = argv[1] if len(argv) > 1 else (os.getenv("PX_TAPE_DIR") or str(root / "data" / "tape"))
    if not os.path.isdir(d):
        print("no such directory: " + d)
        return 1
    print(report(d))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
