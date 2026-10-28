"""Install the context pack into a target repository. Adds; never modifies.

  adopt.py --target DIR [--components a,b] [--modules research,data]
           [--profile shared-tree|branch-per-agent] [--drop-rules 05,07]
           [--force] [--apply] [--allow-placeholders] [--json]

An existing target file is never overwritten: it is skipped and reported.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _packmodel as pm
from _mdtable import TableError, find_tables, replace_table

PACK_ROOT = Path(__file__).resolve().parent.parent

SKIP_TOP = pm.SKIP_TOP   # one home, in _packmodel.py; see the note there
SKIP_REL = ("docs/handoff", "docs/tasks", "docs/memory", "docs/.locks")

# --lite leaves out the rule modules whose substance a capable coding harness already
# enforces on its own: inspect before changing, untrusted content, dependencies, data
# handling, reporting style, tool economy and resource budgets. What stays is what only
# the pack supplies - precedence, safety, secrets, evidence, approval gates, coordination,
# scope, change management, doc integrity and the learning loop. A solo project pays for
# less boot text; adding a module back later is a copy plus an index row.
LITE_DROP = frozenset({"02", "05", "07", "09", "10", "13", "14"})

# The four seed paths replace the pack's own state files wholesale: an adopter must not
# inherit the pack's history. Each seed is derived from the shipped file, so it keeps
# that file's H1, Contract block, rotation markers and archive footer -- the structure
# the target's validator requires -- and only its volatile content is emptied. A shorter
# standalone skeleton dropped the Contract block (three contracts warnings) and pointed
# at archive-YYYY.md, which nothing creates (two paths errors).

BEGIN = "<!-- rotate:begin -->"
END = "<!-- rotate:end -->"

SEED_HANDOFF_ENTRY = """## 2026-01-01T00:00:00+00:00 — ADAPT

**State.** ADAPT
**Did.** ADAPT
**Verified.** ADAPT
**Unverified / open.** ADAPT
**Next.** ADAPT
**Blockers.** ADAPT
**Artifacts.** ADAPT
"""

SEED_TASK_TABLE = """| id | title | status | owner | scope | gate | evidence |
|---|---|---|---|---|---|---|
| T-1 | ADAPT | pending | ADAPT | ADAPT | - | - |
"""

SEED_MEMORY_ROW = "| general | `docs/memory/general.md` | 0 | - | ADAPT |"

SEED_LESSON = """## 2026-01-01 — ADAPT

- **Status:** observed
- **Lesson:** ADAPT
- **Evidence:** ADAPT
"""

SEED_KEYS = (
    "docs/03-handoff.md",
    "docs/04-tasks.md",
    "docs/05-memory.md",
    "docs/memory/general.md",
    "docs/handoff/archive-2026.md",
    "docs/tasks/archive-2026.md",
)


def module_number(name: str) -> str:
    m = re.match(r"^(\d{2})-", name)
    return m.group(1) if m else ""


def module_ref_number(value: str) -> str:
    """The module a table cell names, whether it holds 05, 05-... or a path."""
    v = (value or "").strip().strip(chr(96)).replace(chr(92), "/")
    m = re.search(r"(?:^|/)(\d{2})(?=$|[-./])", v)
    return m.group(1) if m else ""


def valid_name(value: str) -> bool:
    """A plain component/module name: no separators, drive, root, dotfile or .."""
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value)) and ".." not in value


def shipped_rule_numbers():
    return {n for n, _ in pm.Pack(PACK_ROOT).rule_modules()}


def empty_rotation(text: str, body: str) -> str:
    if BEGIN not in text or END not in text:
        return body.rstrip("\n") + "\n"
    pre, rest = text.split(BEGIN, 1)
    _, post = rest.split(END, 1)
    return pre + BEGIN + "\n" + body.rstrip("\n") + "\n" + END + post


def replace_table_rows(text: str, is_header, row: str) -> str:
    lines = text.split("\n")
    out = []
    i = 0
    while i < len(lines):
        if is_header(lines[i]) and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|\s*$", lines[i + 1]):
            out.append(lines[i])
            out.append(lines[i + 1])
            i += 2
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                i += 1
            out.append(row)
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def empty_memory_table(text: str) -> str:
    return replace_table_rows(text, lambda l: l.strip().startswith("| topic |") and "shard" in l,
                              SEED_MEMORY_ROW)


def empty_shard(text: str) -> str:
    out = []
    for line in text.split("\n"):
        if line.startswith("## "):
            break
        out.append(line)
    while out and out[-1].strip() == "":
        out.pop()
    return "\n".join(out) + "\n\n" + SEED_LESSON


def seed_text(key: str) -> str:
    """The shipped file's own structure, emptied of the pack's state."""
    text = (PACK_ROOT / key).read_text(encoding="utf-8")
    if key.startswith("docs/handoff/archive-") or key.startswith("docs/tasks/archive-"):
        return text
    if key == "docs/03-handoff.md":
        return empty_rotation(text, SEED_HANDOFF_ENTRY)
    if key == "docs/04-tasks.md":
        return empty_rotation(text, SEED_TASK_TABLE)
    if key == "docs/05-memory.md":
        return empty_memory_table(text)
    return empty_shard(text)


def pack_files():
    for p in sorted(PACK_ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(PACK_ROOT)
        if rel.parts and rel.parts[0] in SKIP_TOP:
            continue
        key = str(rel).replace(chr(92), "/")
        if key.startswith(SKIP_REL) or p.suffix == ".pyc" or p.name.endswith(".tmp"):
            continue
        yield rel


def plan(target, drop_rules, components, modules):
    actions = []
    for rel in pack_files():
        key = str(rel).replace(chr(92), "/")
        if key in SEED_KEYS:
            continue
        if rel.parts[0] == "rules" and module_number(rel.name) in drop_rules:
            actions.append(("drop", key, "rule module declared inapplicable"))
            continue
        if rel.parts[0] == "modules" and (len(rel.parts) < 2 or rel.parts[1] not in modules):
            actions.append(("drop", key, "module not requested"))
            continue
        if (target / rel).exists():
            actions.append(("skip", key, "target file already exists"))
        else:
            actions.append(("copy", key, ""))
    for key in SEED_KEYS:
        if (target / key).exists():
            actions.append(("skip", key, "target file already exists"))
        else:
            actions.append(("seed", key, "state seeded with one ADAPT entry"))
    for cid in components:
        for key in ("%s/AGENTS.md" % cid, "rules/components/%s.md" % cid):
            if (target / key).exists():
                actions.append(("skip", key, "target file already exists"))
            else:
                actions.append(("create", key, "component skeleton"))
    return actions


def apply_plan(target, actions, components, modules, profile, drop_rules):
    changed = []
    edited = []

    def note(bucket, key):
        if key not in bucket:
            bucket.append(key)

    for kind, key, _ in actions:
        if kind == "drop":
            continue
        dest = target / key
        dest.parent.mkdir(parents=True, exist_ok=True)
        if kind == "copy":
            shutil.copyfile(PACK_ROOT / key, dest)
            note(changed, key)
        elif kind == "seed":
            dest.write_text(seed_text(key), encoding="utf-8", newline="\n")
            note(changed, key)
        elif kind == "create":
            if key.endswith("AGENTS.md"):
                body = (PACK_ROOT / "_templates" / "component-agents.md").read_text(encoding="utf-8")
            else:
                body = "# Component rule: %s\n\nADAPT\n" % Path(key).stem
            dest.write_text(body, encoding="utf-8", newline="\n")
            note(changed, key)
    if components:
        add_component_rows(target, components, edited)
    coord = target / "docs" / "08-coordination.md"
    if coord.is_file():
        text = coord.read_text(encoding="utf-8")
        new = text.replace("SHARED TREE", profile.upper().replace("-", " "), 1)
        if new != text:
            coord.write_text(new, encoding="utf-8", newline="\n")
            note(edited, "docs/08-coordination.md")
    if drop_rules:
        drop_index_rows(target, drop_rules, edited)
    return changed, edited


def add_component_rows(target, components, edited):
    path = target / "AGENTS.md"
    text = path.read_text(encoding="utf-8")
    try:
        table = next((t for t in find_tables(text) if t.has("id")), None)
    except TableError:
        return
    if not table:
        return
    rows = list(table.rows)
    have = {r.get("id", "") for r in rows}
    for cid in components:
        if cid in have:
            continue
        rows.append({"id": cid, "path scope": "%s/" % cid, "scoped entry": "%s/AGENTS.md" % cid,
                     "component rule": "rules/components/%s.md" % cid})
    new = replace_table(text, table, rows)
    if new != text:
        path.write_text(new, encoding="utf-8", newline="\n")
        if "AGENTS.md" not in edited:
            edited.append("AGENTS.md")


def drop_index_rows(target, drop_rules, edited):
    path = target / "rules" / "00-index.md"
    if not path.is_file():
        return
    original = path.read_text(encoding="utf-8")
    text = original
    try:
        tables = find_tables(text)
    except TableError:
        return
    for t in tables:
        if not t.rows:
            continue
        keep = [r for r in t.rows
                if not any(module_ref_number(v) in drop_rules for v in r.values())]
        if len(keep) != len(t.rows):
            text = replace_table(text, t, keep)
    if text != original:
        path.write_text(text, encoding="utf-8", newline="\n")
        if "rules/00-index.md" not in edited:
            edited.append("rules/00-index.md")


def placeholders(target):
    counts = {}
    for p in sorted(target.rglob("*.md")):
        rel = str(p.relative_to(target)).replace(chr(92), "/")
        if rel.split("/")[0] in SKIP_TOP:
            continue
        n = p.read_text(encoding="utf-8", errors="replace").count("ADAPT")
        if n:
            counts[rel] = n
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Install the context pack into a target repository.")
    ap.add_argument("--target", required=True)
    ap.add_argument("--components", default="")
    ap.add_argument("--modules", default="")
    ap.add_argument("--profile", default="shared-tree", choices=("shared-tree", "branch-per-agent"))
    ap.add_argument("--drop-rules", default="")
    ap.add_argument("--lite", action="store_true",
                    help="leave out the rule modules a capable harness already covers: %s"
                         % ", ".join(sorted(LITE_DROP)))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--allow-placeholders", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    target = Path(args.target).resolve()
    if not target.is_dir():
        print("ERROR: target %s is not a directory" % target, file=sys.stderr)
        return 2
    version = PACK_ROOT / "VERSION"
    if not version.is_file():
        print("ERROR: %s is missing; every adopted tree is stamped with it, and reading it "
              "after the copy would leave a half-adopted target behind" % version, file=sys.stderr)
        return 2
    pack_version = version.read_text(encoding="utf-8").strip()
    if (target / "AGENTS.md").exists() and not args.force:
        print("ERROR: %s already has an AGENTS.md; refusing. Re-run with --force to add anyway." % target,
              file=sys.stderr)
        return 2
    components = [c.strip() for c in args.components.split(",") if c.strip()]
    modules = {m.strip() for m in args.modules.split(",") if m.strip()}
    drop = {d.strip() for d in args.drop_rules.split(",") if d.strip()}
    if args.lite:
        drop |= LITE_DROP
    for name in components:
        if not valid_name(name):
            print("ERROR: --components value %r is not a plain component name" % name, file=sys.stderr)
            return 2
    for name in sorted(modules):
        if not valid_name(name):
            print("ERROR: --modules value %r is not a plain module name" % name, file=sys.stderr)
            return 2
    protected = sorted(drop & pm.PROTECTED_RULES)
    if protected:
        print("ERROR: rule modules are protected and cannot be dropped: %s" % ", ".join(protected), file=sys.stderr)
        return 2
    unknown = sorted(d for d in drop if d not in shipped_rule_numbers())
    if unknown:
        print("ERROR: --drop-rules names no shipped rule module: %s" % ", ".join(unknown), file=sys.stderr)
        return 2
    if not (target / ".git").exists():
        print("NOTE: %s is not a git repository; adoption is easier to undo where it is one." % target, file=sys.stderr)
    actions = plan(target, drop, components, modules)
    changed, edited = apply_plan(target, actions, components, modules, args.profile, drop) if args.apply else ([], [])
    counts = placeholders(target) if args.apply else {}
    if args.apply:
        (target / "context-pack.json").write_text(json.dumps({
            "pack": "context-pack",
            "pack_version": pack_version,
            "adopted_at": date.today().isoformat(),
            "components": components, "modules": sorted(modules),
            "profile": args.profile, "lite": args.lite, "dropped_rules": sorted(drop),
        }, indent=2) + "\n", encoding="utf-8")
        if "context-pack.json" not in changed:
            changed.append("context-pack.json")
    written = [k for k in changed if k not in edited]
    summary = {
        "target": str(target), "applied": bool(args.apply), "profile": args.profile, "lite": args.lite,
        "components": components, "modules": sorted(modules), "dropped_rules": sorted(drop),
        "written": len(written), "edited": len(edited),
        "copy": sum(1 for k, _, _ in actions if k == "copy"),
        "skip": sum(1 for k, _, _ in actions if k == "skip"),
        "seed": sum(1 for k, _, _ in actions if k == "seed"),
        "create": sum(1 for k, _, _ in actions if k == "create"),
        "placeholders": counts,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        for kind, key, why in actions:
            if kind in ("drop", "skip", "create"):
                print("%-7s %-46s %s" % (kind, key, why))
        print("%d to copy, %d skipped, %d seeded, %d created"
              % (summary["copy"], summary["skip"], summary["seed"], summary["create"]))
        for rel, n in sorted(counts.items()):
            print("ADAPT   %-46s %d" % (rel, n))
    if args.apply and counts and not args.allow_placeholders:
        print("ERROR: %d file(s) still contain ADAPT placeholders" % len(counts), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
