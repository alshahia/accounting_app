"""Validate a context pack.

Exit codes: 0 clean (warnings allowed), 1 findings, 2 usage or internal error.
Nothing is written except an explicitly requested baseline file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _packmodel as pm
import checks


def load_baseline(path):
    """A missing or malformed baseline is no baseline: every finding is new. Fails closed.
    Returns (fingerprints or None, review_by, pack_version)."""
    if not path:
        return None, "", ""
    p = Path(path)
    if not p.is_file():
        print("WARNING: baseline %s does not exist; treating every finding as new" % path, file=sys.stderr)
        return None, "", ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return (set(data["findings"]),
                str(data.get("review_by", "")),
                str(data.get("pack_version", "")))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        print("WARNING: baseline %s is malformed (%s); treating every finding as new" % (path, exc), file=sys.stderr)
        return None, "", ""


def baseline_warnings(baselined, review_by, pack_version, this_version):
    """Why this baseline cannot be trusted to describe this pack. Never errors: a
    baseline is an aid, not a gate, so the exit code stays `1 if errors else 0` and
    the five --json keys do not change. An empty pack_version means a baseline written
    before the pack had a VERSION, which is not a finding."""
    if baselined is None:
        return []
    notes = []
    if pack_version and pack_version != this_version:
        notes.append("baseline was written for pack version %s, this pack is %s" % (pack_version, this_version))
    if review_by:
        try:
            lapsed = date.fromisoformat(review_by) < date.today()
        except ValueError:
            notes.append("baseline review_by %r does not parse as a date" % review_by)
        else:
            if lapsed:
                notes.append("baseline review_by %s has lapsed" % review_by)
    return notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Validate a context pack.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--baseline", default=None)
    ap.add_argument("--write-baseline", default=None)
    args = ap.parse_args(argv)
    pack = pm.Pack(Path(args.root).resolve())
    if not pack.exists("AGENTS.md"):
        print("ERROR: %s is not a context pack (no AGENTS.md)" % pack.root, file=sys.stderr)
        return 2
    found = checks.run_all(pack)
    if args.write_baseline:
        payload = {
            "pack_version": pack.read("VERSION").strip(),
            "review_by": (date.today() + timedelta(days=30)).isoformat(),
            "findings": sorted({f.fingerprint() for f in found}),
        }
        Path(args.write_baseline).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print("wrote baseline with %d findings, review_by %s" % (len(payload["findings"]), payload["review_by"]))
        return 0
    baselined, review_by, baseline_version = load_baseline(args.baseline)
    new = [f for f in found if not (baselined and f.fingerprint() in baselined)]
    old = len(found) - len(new) if baselined else 0
    errors = [f for f in new if f.severity == "error"]
    warnings = [f for f in new if f.severity == "warning"]
    # Warnings about the baseline itself, never findings and never errors: they cannot
    # change the exit code (`1 if errors else 0`) or --json, which keeps five keys.
    for note in baseline_warnings(baselined, review_by, baseline_version, pack.read("VERSION").strip()):
        print("WARNING: %s" % note, file=sys.stderr)
    if args.json:
        print(json.dumps({
            "new": [{"check": f.check, "severity": f.severity, "path": f.path,
                     "line": f.line, "message": f.message} for f in new],
            "baselined": old, "baseline_review_by": review_by,
            "errors": len(errors), "warnings": len(warnings),
        }, indent=2))
    else:
        for f in sorted(new, key=lambda f: (f.severity != "error", f.path, f.line)):
            where = "%s:%d" % (f.path, f.line) if f.path else "-"
            print("%-8s %-16s %-42s %s" % (f.severity.upper(), f.check, where, f.message))
        if old:
            print("%d finding(s) baselined" % old)
        if review_by:
            print("baseline review_by %s" % review_by)
        print("%d errors, %d warnings" % (len(errors), len(warnings)))
        # The ADAPT report, which is not a finding: it counts the markers a consumer still
        # has to fill in. Forms keep theirs on purpose, so this never changes the exit code
        # and never appears in --json, whose keys are a contract.
        adapt = checks.c00_adapt(pack)
        for f in sorted(adapt, key=lambda f: f.path):
            print("%-9s %-42s %s" % (f.severity.upper(), f.path, f.message))
        print("%d file(s) still carry ADAPT markers" % len(adapt))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
