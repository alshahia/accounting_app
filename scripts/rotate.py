"""Move inline entries into their archives. Idempotent, dry-run by default, never deletes.

  rotate.py [--root DIR] [--apply] [--check] [--year YYYY] [--json]

The only automated content-moving operation in the pack, and it is a move:
an entry that leaves an inline region always lands in an archive first.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _packmodel as pm
from _mdtable import TableError, find_tables, h2_blocks, render, replace_table


class Abort(Exception):
    """Rotation cannot proceed safely. Nothing was written."""


ROTATABLE = ("docs/03-handoff.md", "docs/04-tasks.md")
SUBFOLDER = {"docs/03-handoff.md": "handoff", "docs/04-tasks.md": "tasks"}
ARCHIVE_TITLE = {"docs/03-handoff.md": "Handoff archive", "docs/04-tasks.md": "Task archive"}


def archive_path(rel: str, year: int) -> str:
    folder = rel.rsplit("/", 1)[0]
    return "%s/%s/archive-%d.md" % (folder, SUBFOLDER[rel], year)


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def plan_blocks(pack: pm.Pack, rel: str, cap: int, keep: int):
    """Return (all entries newest-first, the entries to move). Raises Abort on anything unparseable."""
    entries = pack.entries(rel)
    if not entries:
        raise Abort("%s: rotation markers missing or region empty" % rel)
    missing = [e.title for e in entries if e.ts is None]
    if missing:
        raise Abort("%s: entry without an ISO-8601 timestamp: %r" % (rel, missing[0]))
    oversized = [(e.title, _bytes(e.text)) for e in entries if _bytes(e.text) > pm.ENTRY_CAP]
    if oversized:
        raise Abort("%s: entry over %d bytes (%d): %r"
                    % (rel, pm.ENTRY_CAP, oversized[0][1], oversized[0][0]))
    ordered = sorted(entries, key=lambda e: e.ts, reverse=True)
    total = sum(_bytes(e.text) + 2 for e in ordered)
    # The entries that overflow are the OLDEST, so they come off the end of a
    # newest-first list. Walking forward from `keep` instead moved the fourth-newest
    # entry first and stopped as soon as the remainder fit, which leaves the oldest
    # entry inline while newer ones sit in the archive -- the live handoff would
    # then show a stale entry as if it were current, and nothing would ever move it.
    # Three entries always fit: 3 * ENTRY_CAP + overhead < the handoff cap.
    move = []
    i = len(ordered) - 1
    while i >= keep and total > cap:
        e = ordered[i]
        total -= _bytes(e.text) + 2
        move.append(e)
        i -= 1
    move.reverse()
    return ordered, move


def _archive_text(pack: pm.Pack, rel: str, archive_rel: str, fresh: list, year: int) -> str:
    """The archive with this batch above everything already archived.

    The archive carries the same convention as the live region, so the batch just
    rotated goes ABOVE everything already archived: it is older than the live
    entries but newer than every earlier rotation. Appending, as this did, leaves
    the file in oldest-first order while its own header claims the opposite.
    """
    archive = pack.read(archive_rel).rstrip("\n")
    if not archive:
        archive = "# %s -- %d\n\nRotated entries, newest first. Rotation moves; it never deletes." % (
            ARCHIVE_TITLE[rel], year)
    lines = archive.split("\n")
    cut = next((n for n, line in enumerate(lines) if line.startswith("## ")), len(lines))
    head = "\n".join(lines[:cut]).rstrip("\n")
    tail = "\n".join(lines[cut:]).strip("\n")
    out = head + "\n\n" + "\n\n".join(e.text for e in fresh)
    if tail:
        out += "\n\n" + tail
    return out + "\n"

def _rotate_blocks(pack: pm.Pack, rel: str, archive_rel: str, apply: bool, year: int) -> dict:
    ordered, move = plan_blocks(pack, rel, pm.CAPS[rel], pm.NEWEST_ENTRIES)
    have = {title for title, _ in h2_blocks(pack.read(archive_rel))}
    fresh = [e for e in move if e.title not in have]
    # Every planned move leaves the live region; only the ones the archive does not already
    # hold are written into it. Keeping the already-archived ones inline, as this did, leaves
    # the region over its cap while the copy that would bring it back under already sits in
    # the archive - so `--check` can stay red forever. The rows path below already behaves
    # this way: `live` drops every done row and the id check only guards the archive write.
    moved = {e.title for e in move}
    kept = [e for e in ordered if e.title not in moved]
    res = {"target": rel, "archive": archive_rel, "kept": len(kept), "moved": len(fresh),
           "already_archived": len(move) - len(fresh),
           "inline_bytes": sum(_bytes(e.text) + 2 for e in kept)}
    if not move:
        res["status"] = "no-op"
        return res
    res["status"] = "applied" if apply else "dry-run"
    if not apply:
        return res
    text = pack.read(rel)
    head, rest = text.split(pm.BEGIN, 1)
    _, tail = rest.split(pm.END, 1)
    new_inline = head + pm.BEGIN + "\n" + "\n\n".join(e.text for e in kept).strip("\n") + "\n" + pm.END + tail
    # Only the entries the archive does not already hold go into it; the live region is
    # rewritten either way, because an entry kept inline while its copy already sits in
    # the archive leaves `--check` red forever.
    if fresh:
        _write(pack.path(archive_rel), _archive_text(pack, rel, archive_rel, fresh, year))
    try:
        _write(pack.path(rel), new_inline)
    except OSError as exc:
        _write(pack.path(rel), text)
        raise Abort("%s: write failed, original bytes restored: %s" % (rel, exc))
    return res


def _rotate_rows(pack: pm.Pack, rel: str, archive_rel: str, apply: bool, year: int) -> dict:
    try:
        tables = [t for t in pack.tables(rel) if t.has("id", "status")]
    except TableError as exc:
        raise Abort("%s: %s" % (rel, exc))
    if not tables:
        raise Abort("%s: no table with id and status columns" % rel)
    table = tables[0]
    done = [r for r in table.rows if r.get("status", "").strip() == "done"]
    live = [r for r in table.rows if r.get("status", "").strip() != "done"]
    res = {"target": rel, "archive": archive_rel, "kept": len(live), "moved": len(done)}
    if not done:
        res["status"] = "no-op"
        return res
    res["status"] = "applied" if apply else "dry-run"
    if not apply:
        return res
    archive_text = pack.read(archive_rel)
    if not archive_text.strip():
        new_archive = "# %s -- %d\n\n" % (ARCHIVE_TITLE[rel], year) + "\n".join(render(table.header, done)) + "\n"
    else:
        try:
            atables = [t for t in find_tables(archive_text) if t.header == table.header]
        except TableError as exc:
            raise Abort("%s: %s" % (archive_rel, exc))
        if atables:
            at = atables[0]
            have = {r.get("id", "") for r in at.rows}
            new_archive = replace_table(archive_text, at, at.rows + [r for r in done if r.get("id", "") not in have])
        else:
            # A seeded archive is an H1 and a purpose line with no table, because
            # that is what this plan seeds in Task 6. The first genuine move is
            # when it acquires a table, so append one rather than aborting on the
            # state the pack itself creates. A table with a *different* header
            # still aborts: that is real corruption, not a seed.
            new_archive = archive_text.rstrip() + "\n\n" + "\n".join(render(table.header, done)) + "\n"
    original = pack.read(rel)
    new_inline = replace_table(original, table, live)
    _write(pack.path(archive_rel), new_archive)
    try:
        _write(pack.path(rel), new_inline)
    except OSError as exc:
        _write(pack.path(rel), original)
        raise Abort("%s: write failed, original bytes restored: %s" % (rel, exc))
    return res


def _rotator(rel: str):
    """The planner/writer for one rotatable file. Both take the same arguments."""
    return _rotate_blocks if rel == "docs/03-handoff.md" else _rotate_rows


def run(pack: pm.Pack, apply: bool = False, year: int | None = None) -> list[dict]:
    year = year or datetime.now().year
    # Plan every target before writing any of them. Each planner reads and parses its whole
    # file and raises Abort on anything it cannot read, so planning them all first means a
    # malformed table aborts with nothing written. Writing as each plan succeeded, as this
    # did, left the handoff rotated in place when the tasks table below it turned out ragged.
    for rel in ROTATABLE:
        if pack.exists(rel):
            _rotator(rel)(pack, rel, archive_path(rel, year), False, year)
    out = []
    for rel in ROTATABLE:
        if not pack.exists(rel):
            out.append({"target": rel, "status": "missing", "moved": 0, "kept": 0})
            continue
        out.append(_rotator(rel)(pack, rel, archive_path(rel, year), apply, year))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Move inline entries into archives (never deletes).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    ap.add_argument("--check", action="store_true", help="exit 1 if any inline region is over cap")
    ap.add_argument("--year", type=int, default=None, help="archive year (default: this year)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    pack = pm.Pack(Path(args.root).resolve())
    if args.check:
        over = [(rel, pack.capped_size(rel), cap) for rel, cap in pm.CAPS.items()
                if pack.exists(rel) and pack.capped_size(rel) > cap]
        if args.json:
            print(json.dumps({"over_cap": over}, indent=2))
        for rel, size, cap in over:
            print("OVER CAP %s: %d > %d" % (rel, size, cap))
        return 1 if over else 0
    try:
        results = run(pack, apply=args.apply, year=args.year)
    except Abort as exc:
        print("ABORT: %s" % exc, file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(results, indent=2))
    for r in results:
        print("%-22s %-8s moved=%d kept=%d" % (r["target"], r.get("status", "?"), r.get("moved", 0), r.get("kept", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
