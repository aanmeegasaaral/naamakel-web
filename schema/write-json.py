#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Write a catalog file in the ONE canonical form.

    cat something.json | python3 schema/write-json.py data/songs.json

WHY THIS EXISTS
---------------
These files are edited by two things now: the Python tools here, and the PHP
admin. Python writes them with `indent=2, ensure_ascii=False`; PHP's
JSON_PRETTY_PRINT uses FOUR spaces and, without flags, escapes every Tamil
character to \\uXXXX.

Left alone, the first save from the admin would reformat all 15 songs and turn
every future `git diff` into noise. That matters more than it sounds: the whole
publish design rests on a human reading the diff before it reaches every
installed phone, and a diff nobody can read is a review that does not happen.

So neither side formats. Both pipe through here.

Refuses to write anything it cannot parse, and writes atomically, so an
interrupted save cannot leave a half-written catalog on disk.
"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def canonical(obj) -> str:
    """
    2-space indent, real Tamil rather than escapes, trailing newline, LF.

    Key order is NOT sorted -- it is whatever the caller supplied. Sorting would
    reorder every existing file on first write, which is the exact diff-noise
    this is here to prevent. The caller preserves order by editing the decoded
    document in place instead of rebuilding it.
    """
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: write-json.py <target-file>   (JSON on stdin)", file=sys.stderr)
        return 2
    target = sys.argv[1]

    raw = sys.stdin.buffer.read().decode("utf-8")
    try:
        obj = json.loads(raw)
    except ValueError as e:
        # Never write. A caller that produced bad JSON has a bug, and silently
        # truncating the live catalog would turn that bug into an outage.
        print("refusing to write %s: input is not valid JSON: %s" % (target, e), file=sys.stderr)
        return 1

    if not isinstance(obj, dict):
        print("refusing to write %s: top level must be an object" % target, file=sys.stderr)
        return 1

    text = canonical(obj)

    # Atomic: write beside the target, then rename over it. A crash mid-write
    # otherwise leaves a truncated file that the app would reject on next fetch.
    tmp = target + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, target)

    print("wrote %s (%d bytes)" % (target, len(text.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
