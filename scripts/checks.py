"""The context-pack checks. Pure functions over a Pack. Stdlib only."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import _packmodel as pm
from _mdtable import TableError, find_tables, h2_blocks

STRENGTH = re.compile(r"\b(MUST|SHOULD|MAY)\b")
STATUS_LINE = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*)?Status(?:\*\*)?\s*:\s*([A-Za-z_]+)", re.I)
SECRET = re.compile(
    r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*['\"]?([A-Za-z0-9/+_\-]{16,})")
SENTENCE = re.compile(r"[^.!?\n]{60,}?\.")

# A comment is not the pack's prose. The pack keeps its machine markers there
# (`<!-- rotate:begin -->`) and _templates/ states a form's own instructions there,
# and a form copied into a scoped AGENTS.md is *meant* to repeat its template's comment
# verbatim. Scanning comments makes every adopted tree report duplicate-facts warnings
# about the form's boilerplate: a warning that arrives with the copy, describes the
# design rather than a mistake, and can never be acted on.
COMMENT = re.compile(r"<!--.*?-->", re.S)

# A rule is *defined* where its bold heading states it, and only *cited* everywhere
# else. rules/00-index.md says so itself: "Cite rules by ID from any other document".
# Matching every occurrence instead made the index look like module 00 defining
# R-03.2, and then made rules/03 look like a second definition of its own rule.
RULE_DEF = re.compile(r"\*\*(R-\d{2}\.\d+)\s*—")

# A lesson heading is dated by a bare calendar date (docs/00-schema.md), unlike a
# handoff entry, which carries a full ISO-8601 timestamp. parse_ts deliberately
# returns None for a bare date, so a shard ordering check routed through Pack.entries
# would drop every lesson and pass no matter how the shard was ordered.
DATE_HEAD = re.compile(r"^(\d{4}-\d{2}-\d{2})\b")

TASK_STATUS = {"pending", "in_progress", "blocked", "done"}
PLAN_STATUS = {"draft", "approved", "in_progress", "done", "abandoned"}
DECISION_STATUS = {"proposed", "accepted", "superseded", "rejected"}
LESSON_STATUS = {"observed", "candidate", "validated", "promoted", "rejected"}

@dataclass(frozen=True)
class Finding:
    check: str
    severity: str
    path: str
    line: int
    message: str
    key: str = ""

    def fingerprint(self) -> str:
        # A finding's identity is its exact text, digits included. Normalizing digits
        # collapsed `T-001` and `T-002` - and `R-01.4` and `R-02.4` - into one
        # fingerprint, so a baseline recorded for one violation silently absorbed the
        # other, and a new violation could never fail the run. Digits are identity for
        # task ids, rule ids and duplicate titles; they are noise only for a measured
        # value, and the call site says which it is by passing `key`. A regex cannot
        # tell the two apart, which is why it must not try.
        return "%s|%s|%d|%s" % (self.check, self.path, self.line, self.key or self.message)


def F(check, severity, path, line, message, key=None):
    """`key` stands in for a message that embeds a value which legitimately changes -
    a byte count, a freshness age - so that finding keeps one identity across edits.
    It defaults to the message, so the message is the identity wherever it is stable."""
    return Finding(check, severity, path, line, message, key if key is not None else message)


def walk(pack):
    for p in sorted(pack.root.rglob("*")):
        if not p.is_file():
            continue
        if set(p.relative_to(pack.root).parts) & pm.SKIP_TOP:
            continue
        yield p


def table_with(pack, rel, *columns):
    """First table in `rel` carrying every named column, ignoring case.

    Row keys are lowercase (see `_mdtable.find_tables`) and a header renders the name the way
    its own file writes it: docs/05-memory.md and docs/04-tasks.md are lowercase,
    docs/02-environment.md is title-case because docs/00-schema.md declares `Last verified` and
    `Verified by` that way. Comparing literally read that table as absent, which left c05
    checking nothing and c06 warning about a table that exists.
    """
    for t in pack.tables(rel):
        if t.has(*columns):
            return t
    return None


# --- ported from v1 (unchanged behaviour) -------------------------------------------------
ENTRY_FILES = ("AGENTS.md", "CLAUDE.md")
RULE_MODULE_REF = re.compile(r"rules/(\d{2}-[a-z0-9\-]+\.md)")
PATH_REF = re.compile(r"(?<![\w/.\-])((?:rules|docs|_templates|scripts)/[A-Za-z0-9_./\-]*\.(?:md|py))")
MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
ADAPT = re.compile(r"\bADAPT\b")


def c00_required(pack):
    """Every file the pack declares required is present."""
    return [F("required", "error", rel, 0, "missing required file")
            for rel in pm.REQUIRED if not pack.exists(rel)]


def c00_index(pack):
    """Every rules module the registry names exists, and every module is named."""
    refs = set()
    for rel in ("CLAUDE.md", "rules/00-index.md"):
        if pack.exists(rel):
            refs |= set(RULE_MODULE_REF.findall(pack.read(rel)))
    out = []
    if not refs:
        out.append(F("index", "error", "rules/00-index.md", 0,
                     "no rules modules referenced from CLAUDE.md or rules/00-index.md"))
    for ref in sorted(refs):
        if not pack.exists("rules/" + ref):
            out.append(F("index", "error", "rules/" + ref, 0,
                         "referenced rules module does not exist"))
    for _num, name in pack.rule_modules():
        if name != "00-index.md" and name not in refs:
            out.append(F("index", "warning", "rules/" + name, 0,
                         "exists but is not indexed in CLAUDE.md or rules/00-index.md"))
    return out


def c00_paths(pack):
    """Every path-looking reference in the entry files, docs/ and rules/ resolves."""
    docs = pack.root / "docs"
    rules = pack.root / "rules"
    targets = list(ENTRY_FILES)
    if docs.is_dir():
        targets += sorted(p.relative_to(pack.root).as_posix() for p in docs.rglob("*.md"))
    if rules.is_dir():
        targets += sorted(p.relative_to(pack.root).as_posix() for p in rules.glob("*.md"))
    out = []
    for rel in targets:
        if not pack.exists(rel):
            continue
        text = MD_LINK.sub(" ", pack.read(rel))
        for ref in sorted(set(PATH_REF.findall(text))):
            ref = ref.rstrip(".,;:)")
            if "*" in ref or "?" in ref:
                continue
            if not (pack.root / ref).exists():
                out.append(F("paths", "error", rel, 0,
                             "references a path that does not exist: %s" % ref))
    return out


def c00_contracts(pack):
    """Every pack-owned docs/ file declares a **Contract** block, minus two exempt classes."""
    docs = pack.root / "docs"
    if not docs.is_dir():
        return []
    out = []
    for p in sorted(docs.rglob("*.md")):
        rel = p.relative_to(pack.root).as_posix()
        if "/_" in rel or Path(rel).name.startswith("_"):
            continue
        if "0000-template" in rel:
            continue
        # Only the pack's own numbered documents and their directories owe a Contract.
        # Adopting into a project that already keeps docs/ must not turn every one of
        # that project's documents into a warning the pack has no right to raise.
        if not re.match(r"docs/\d{2}-[^/]+(\.md$|/)", rel):
            continue
        # Two exempt classes, both pure volatile content with no change control of
        # their own, so a Contract block could never be kept true here:
        #  * docs/*/archive-*.md — seeded as an H1 plus a one-line purpose and
        #    append-only, so a Contract block would be restated on every rotation.
        #  * docs/memory/*.md — the memory subsystem's Mutability line is owned once
        #    by docs/05-memory.md, whose registry reaches every shard; repeating it in
        #    each topic shard is a second copy of a fact for every topic an adopter
        #    creates, the defect this pack exists to remove.
        # Every other file under docs/ keeps the requirement unchanged:
        # docs/09-decisions/README.md and docs/10-runbooks/README.md still carry theirs.
        if rel.startswith("docs/memory/") or re.fullmatch(r"docs/[^/]+/archive-[^/]*\.md", rel):
            continue
        if "Contract" not in pack.read(rel):
            out.append(F("contracts", "warning", rel, 0, "does not declare a Contract block"))
    return out


def c00_adapt(pack):
    """Remaining ADAPT markers in the shipped pack, not in its build scaffolding."""
    out = []
    for p in sorted(pack.root.rglob("*.md")):
        rel = p.relative_to(pack.root).as_posix()
        # Skip build scaffolding before counting: _dev/ holds this pack's own plan and
        # design record, .superpowers/ its scratch briefs, so counting their markers
        # describes the plan that built the pack instead of the pack. SKIP_TOP is the
        # same set adopt.py uses to decide what ships. Forms keep their markers and must
        # keep counting: _templates/ and docs/**/_template.md exist to hold placeholders.
        if rel.split("/")[0] in pm.SKIP_TOP:
            continue
        n = len(ADAPT.findall(pack.read(rel)))
        if n:
            out.append(F("adapt", "info", rel, 0, "%d ADAPT marker(s) remain" % n))
    return out


# --- new checks ---------------------------------------------------------------------------
def c01_adapter_purity(pack):
    out = []
    for rel in pm.ADAPTERS:
        if not pack.exists(rel):
            out.append(F("adapter", "error", rel, 0, "adapter file is missing"))
            continue
        size = pack.size(rel)
        if size > pm.ADAPTER_CAP:
            out.append(F("adapter", "error", rel, 0, "%d bytes exceeds the %d cap" % (size, pm.ADAPTER_CAP),
                         "adapter file exceeds its byte cap"))
        text = pack.read(rel)
        if "AGENTS.md" not in text:
            out.append(F("adapter", "error", rel, 0, "does not point at AGENTS.md"))
        ids = pm.rule_ids(text)
        if ids:
            out.append(F("adapter", "error", rel, 0, "contains rule ids: %s" % ", ".join(ids[:3])))
        strengths = sorted(set(STRENGTH.findall(text)))
        if strengths:
            out.append(F("adapter", "error", rel, 0, "states obligations (%s); adapters carry no policy" % ", ".join(strengths)))
    return out


def c02_scoped_entries(pack):
    out = []
    declared = set()
    for r in pack.component_rows():
        for key in ("scoped entry", "scoped_entry", "scoped"):
            if r.get(key):
                declared.add(r[key].strip().strip(chr(96)))
    for rel in pack.scoped_agents():
        size = pack.size(rel)
        if size > pm.SCOPED_CAP:
            out.append(F("scoped", "error", rel, 0, "%d bytes exceeds the %d cap" % (size, pm.SCOPED_CAP),
                         "scoped entry exceeds its byte cap"))
        text = pack.read(rel)
        if not re.search(r"(?i)smallest\s+validation", text):
            out.append(F("scoped", "error", rel, 0, "declares no smallest validation command"))
        strengths = sorted(set(STRENGTH.findall(text)))
        if strengths:
            out.append(F("scoped", "error", rel, 0, "restates root obligations (%s)" % ", ".join(strengths)))
        if rel not in declared:
            out.append(F("scoped", "error", rel, 0, "has no row in the AGENTS.md component table"))
    for d in sorted(declared):
        if d and not pack.exists(d):
            out.append(F("scoped", "error", "AGENTS.md", 0, "component table names a missing file: %s" % d))
    return out


def c03_vocabulary(pack):
    out = []
    t = table_with(pack, "docs/04-tasks.md", "id", "status")
    if t:
        for r in t.rows:
            v = r.get("status", "").strip()
            if v and v not in TASK_STATUS:
                out.append(F("vocabulary", "error", "docs/04-tasks.md", t.line(),
                             "unknown task status %r in row %s" % (v, r.get("id"))))
    for p in sorted((pack.root / "docs" / "09-decisions").glob("*.md")):
        rel = "docs/09-decisions/" + p.name
        if p.name.startswith("0000"):
            continue
        for n, line in enumerate(pack.read(rel).split("\n"), 1):
            m = STATUS_LINE.match(line)
            if m and m.group(1).lower() not in DECISION_STATUS:
                out.append(F("vocabulary", "error", rel, n, "unknown decision status %r" % m.group(1)))
    for p in sorted((pack.root / "docs" / "memory").glob("*.md")):
        rel = "docs/memory/" + p.name
        for n, line in enumerate(pack.read(rel).split("\n"), 1):
            m = STATUS_LINE.match(line)
            if m and m.group(1).lower() not in LESSON_STATUS:
                out.append(F("vocabulary", "error", rel, n, "unknown lesson status %r" % m.group(1)))
    known = PLAN_STATUS | TASK_STATUS | LESSON_STATUS | DECISION_STATUS
    for p in sorted((pack.root / "_templates").glob("*.md")):
        rel = "_templates/" + p.name
        for n, line in enumerate(pack.read(rel).split("\n"), 1):
            m = STATUS_LINE.match(line)
            if m and m.group(1).lower() not in known:
                out.append(F("vocabulary", "error", rel, n, "unknown form status %r" % m.group(1)))
    return out


def c04_evidence(pack):
    out = []
    t = table_with(pack, "docs/04-tasks.md", "id", "status", "evidence")
    if not t:
        return [F("evidence", "error", "docs/04-tasks.md", 0, "no task table with id, status and evidence columns")]
    for r in t.rows:
        status = r.get("status", "").strip()
        raw = r.get("evidence", "").strip().strip(chr(96))
        gate = r.get("gate", "").strip().strip(chr(96))
        if status == "done":
            if not raw or raw in {"-", "--", "none", "n/a"}:
                out.append(F("evidence", "error", "docs/04-tasks.md", t.line(),
                             "task %s is done with no evidence path" % r.get("id")))
            elif not (pack.root / raw).exists():
                out.append(F("evidence", "error", "docs/04-tasks.md", t.line(),
                             "task %s cites a path that does not exist: %s" % (r.get("id"), raw)))
        if status == "blocked" and (not gate or gate in {"-", "--", "none", "n/a"}):
            out.append(F("evidence", "error", "docs/04-tasks.md", t.line(),
                         "task %s is blocked and names no gate" % r.get("id")))
    return out


def c05_timestamps(pack):
    out = []
    for e in pack.entries("docs/03-handoff.md"):
        if e.ts is None:
            out.append(F("timestamps", "error", "docs/03-handoff.md", 0,
                         "entry without an ISO-8601 timestamp with offset: %r" % e.title))
    t = table_with(pack, "docs/02-environment.md", "last verified")
    if t:
        for r in t.rows:
            raw = r.get("last verified", "")
            if raw and pm.parse_ts(raw) is None:
                out.append(F("timestamps", "error", "docs/02-environment.md", t.line(),
                             "unparseable Last verified: %r" % raw))
    return out


def c06_freshness(pack):
    now = datetime.now().astimezone()
    t = table_with(pack, "docs/02-environment.md", "last verified", "verified by")
    if not t:
        return [F("freshness", "warning", "docs/02-environment.md", 0,
                  "no environment table with Last verified and Verified by columns")]
    out = []
    for r in t.rows:
        # A row that still carries the template's ADAPT marker states no fact yet,
        # and c01 already reports those. A freshness warning on a fact nobody has
        # stated is permanently true on the template, and a warning nobody can act
        # on is a warning nobody reads.
        if any("ADAPT" in v for v in r.values()):
            continue
        ts = pm.parse_ts(r.get("last verified", ""))
        if ts is None:
            out.append(F("freshness", "warning", "docs/02-environment.md", t.line(), "row has no verifiable timestamp"))
            continue
        if not r.get("verified by", "").strip():
            out.append(F("freshness", "warning", "docs/02-environment.md", t.line(), "row has no Verified by"))
        digits = re.sub(r"\D", "", r.get("review days", "") or "30")
        window = int(digits) if digits else 30
        age = (now - ts).days
        if age > window:
            out.append(F("freshness", "warning", "docs/02-environment.md", t.line(),
                         "STALE: %s days old, window is %d days" % (age, window),
                         "the row's Last verified is older than its window"))
    return out


def c07_ordering(pack):
    """Every ordering convention docs/00-schema.md declares, not only the two the v1
    pack happened to have. The schema also declares memory shards oldest-first and the
    memory topic registry newest-updated first; enforcing half a declared convention is
    how the drift it describes comes back."""
    out = []
    stamped = [e for e in pack.entries("docs/03-handoff.md") if e.ts]
    if stamped != sorted(stamped, key=lambda e: e.ts, reverse=True):
        out.append(F("ordering", "error", "docs/03-handoff.md", 0, "entries are not newest-first"))
    rel = "docs/09-decisions/README.md"
    if pack.exists(rel):
        for t in pack.tables(rel):
            if t.has("id"):
                nums = [int(r["id"]) for r in t.rows if re.fullmatch(r"\d+", r.get("id", ""))]
                if nums and nums != sorted(nums, reverse=True):
                    out.append(F("ordering", "error", rel, t.line(), "decision index is not newest-first"))
    t = table_with(pack, "docs/05-memory.md", "topic", "last updated")
    if t:
        stamps = [v for v in (r.get("last updated", "").strip() for r in t.rows)
                  if DATE_HEAD.match(v)]
        if stamps != sorted(stamps, reverse=True):
            out.append(F("ordering", "error", "docs/05-memory.md", t.line(),
                         "registry rows are not newest-updated first: %s" % ", ".join(stamps)))
    shards = pack.root / "docs" / "memory"
    if shards.is_dir():
        for p in sorted(shards.glob("*.md")):
            srel = "docs/memory/" + p.name
            dates = [DATE_HEAD.match(title).group(1)
                     for title, _ in h2_blocks(pack.read(srel)) if DATE_HEAD.match(title)]
            if dates != sorted(dates):
                out.append(F("ordering", "error", srel, 0,
                             "lesson shards are oldest-first: %s" % ", ".join(dates)))
    return out


def c08_archives(pack):
    out = []
    inline = {e.title for e in pack.entries("docs/03-handoff.md")}
    archived = set()
    for p in sorted((pack.root / "docs" / "handoff").glob("archive-*.md")):
        archived |= {t for t, _ in h2_blocks(pack.read("docs/handoff/" + p.name))}
    for title in sorted(inline & archived):
        out.append(F("archives", "error", "docs/03-handoff.md", 0, "entry is in two homes: %r" % title))
    if pack.exists("docs/04-tasks.md"):
        t = table_with(pack, "docs/04-tasks.md", "id")
        live = {r.get("id", "") for r in t.rows} if t else set()
        arch = set()
        for p in sorted((pack.root / "docs" / "tasks").glob("archive-*.md")):
            try:
                for at in find_tables(pack.read("docs/tasks/" + p.name)):
                    arch |= {r.get("id", "") for r in at.rows}
            except TableError as exc:
                out.append(F("archives", "error", "docs/tasks/" + p.name, 0, str(exc)))
        for tid in sorted(live & arch):
            out.append(F("archives", "error", "docs/04-tasks.md", 0, "task is in two homes: %s" % tid))
    return out


def c09_inline_caps(pack):
    out = []
    for rel, cap in pm.CAPS.items():
        if pack.exists(rel) and pack.capped_size(rel) > cap:
            out.append(F("caps", "error", rel, 0, "%d bytes exceeds the %d cap" % (pack.capped_size(rel), cap),
                         "file exceeds its inline byte cap"))
    total = sum(pack.capped_size(r) for r in ("SOUL.md", "AGENTS.md", "docs/03-handoff.md",
                                              "docs/04-tasks.md", "docs/08-coordination.md") if pack.exists(r))
    if total > pm.BOOT_TOTAL:
        out.append(F("caps", "error", "AGENTS.md", 0,
                     "boot set is %d bytes, over the %d cap" % (total, pm.BOOT_TOTAL),
                     "boot set exceeds its byte cap"))
    return out


def c10_rule_ids(pack):
    out = []
    seen = {}
    for num, name in pack.rule_modules():
        rel = "rules/" + name
        for line in pack.read(rel).split("\n"):
            ids = RULE_DEF.findall(line)
            # "Retired IDs resolvable" (spec 7.2 row 10): a retired rule keeps its ID
            # in place, marked *(retired)*, so a citation written before the retirement
            # still resolves. No rule is retired today; the clause stays real anyway.
            if "(retired)" in line:
                ids += [rid for rid in pm.rule_ids(line) if rid not in ids]
            for rid in ids:
                if not rid.startswith("R-%s." % num):
                    out.append(F("rule-ids", "error", rel, 0, "%s lives in module %s" % (rid, num)))
                if rid in seen and seen[rid] != rel:
                    out.append(F("rule-ids", "error", rel, 0, "%s is also defined in %s" % (rid, seen[rid])))
                seen[rid] = rel
    titles = {t for t, _ in h2_blocks(pack.read("AGENTS.md"))} if pack.exists("AGENTS.md") else set()
    for p in walk(pack):
        if p.suffix != ".md" or p.name.startswith(("PLAN-", "DESIGN-")):
            continue
        rel = str(p.relative_to(pack.root)).replace(chr(92), "/")
        for n, line in enumerate(pack.read(rel).split("\n"), 1):
            for rid in pm.rule_ids(line):
                if rid not in seen:
                    out.append(F("rule-ids", "error", rel, n, "cites an undefined rule: %s" % rid))
            if re.search(r"AGENTS\.md\s*§\s*\d", line):
                out.append(F("rule-ids", "error", rel, n,
                             "a section number cannot survive a rewrite; cite the section title"))
            for title in pm.AGENTS_TITLE.findall(line):
                if titles and title not in titles:
                    out.append(F("rule-ids", "error", rel, n,
                                 "cites a missing AGENTS.md section: %r" % title))
    return out


def c11_duplicate_facts(pack):
    seen = {}
    for p in walk(pack):
        if p.suffix != ".md" or p.name.startswith(("PLAN-", "DESIGN-")):
            continue
        rel = str(p.relative_to(pack.root)).replace(chr(92), "/")
        if rel == "AGENTS.md":
            continue
        # Line count is preserved so a reported line still points at the right row.
        text = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), pack.read(rel))
        for n, line in enumerate(text.split("\n"), 1):
            s = line.strip()
            if not s or s[0] in "|#>_" or len(s.split()) < 12:
                continue
            for m in SENTENCE.finditer(s):
                key = re.sub(r"\s+", " ", re.sub(r"[*_]", "", m.group(0))).strip().lower()
                seen.setdefault(key, set()).add("%s:%d" % (rel, n))
    out = []
    adapters = set(pm.ADAPTERS)
    for key, homes in sorted(seen.items()):
        files = {h.split(":")[0] for h in homes}
        # The four adapters are required to carry byte-identical bodies (Task 5 Step 4
        # asserts it), so one pointer sentence in all four of them is the design, not a
        # duplicated fact. An adapter repeating a *document's* sentence is still a finding.
        if len(files) >= 2 and not files <= adapters:
            out.append(F("duplicate-facts", "warning", sorted(files)[0], 0,
                         "this sentence also appears in %s: %r" % (", ".join(sorted(files)[1:]), key[:70])))
    return out[:20]


def c12_secrets(pack):
    out = []
    for p in walk(pack):
        rel = str(p.relative_to(pack.root)).replace(chr(92), "/")
        if p.name in (".env", ".env.local", "secrets.yaml", "credentials.json"):
            out.append(F("secrets", "error", rel, 0, "a secrets file must never be part of the pack"))
            continue
        if p.suffix not in (".md", ".py", ".json", ".yml", ".yaml", ".txt"):
            continue
        for n, line in enumerate(pack.read(rel).split("\n"), 1):
            if SECRET.search(line):
                out.append(F("secrets", "error", rel, n, "possible literal secret"))
    return out


def c13_locks(pack):
    locks_dir = pack.root / "docs" / ".locks"
    if not locks_dir.is_dir():
        return []
    locks = sorted(locks_dir.glob("*.lock"))
    if not locks:
        return []
    t = table_with(pack, "docs/08-coordination.md", "owner")
    owners = {r.get("owner", "").strip() for r in t.rows} if t else set()
    out = []
    for lock in locks:
        owner = lock.read_text(encoding="utf-8", errors="replace").strip().split("\n")[0]
        if owner and owner not in owners:
            out.append(F("locks", "warning", "docs/.locks/" + lock.name, 0,
                         "lock held by %r with no matching claim row" % owner))
    return out


# How many commits may land after the handoff was last committed before the validator
# says so. Zero would fire between a code commit and the session-end handoff commit,
# which is the normal order of work; a few commits with no handoff is a session that
# ended without writing one, and that is the drift this check exists to surface.
HANDOFF_LAG = 3


def _git(root, *args):
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def c14_handoff_lag(pack):
    """The handoff trails the git history: commits landed and no session recorded them."""
    rel = "docs/03-handoff.md"
    if not pack.exists(rel) or _git(pack.root, "rev-parse", "--is-inside-work-tree") != "true":
        return []
    # An uncommitted edit to the handoff is a session writing it right now.
    if _git(pack.root, "status", "--porcelain", "--", rel):
        return []
    last = _git(pack.root, "log", "-1", "--format=%H", "--", rel)
    if not last:
        return []
    behind = _git(pack.root, "rev-list", "--count", "%s..HEAD" % last)
    if not behind or not behind.isdigit() or int(behind) <= HANDOFF_LAG:
        return []
    return [F("handoff-lag", "warning", rel, 0,
              "%s commits landed after the handoff was last updated; write an entry for them" % behind,
              "commits landed after the handoff was last updated")]


# The four ported checks that assert the pack is complete and internally consistent run in
# every pass. `c00_adapt` is deliberately not in this tuple: its findings are `info` - the
# count of markers a consumer still has to fill in - so `validate_context.py` prints them as
# part of its report and they never count as an error or a warning. Forms keep their markers
# on purpose, so a non-zero count there is correct and is not a finding.
ALL = (
    c00_required,
    c00_index,
    c00_paths,
    c00_contracts,
    c01_adapter_purity,
    c02_scoped_entries,
    c03_vocabulary,
    c04_evidence,
    c05_timestamps,
    c06_freshness,
    c07_ordering,
    c08_archives,
    c09_inline_caps,
    c10_rule_ids,
    c11_duplicate_facts,
    c12_secrets,
    c13_locks,
    c14_handoff_lag,
)


def run_all(pack):
    out = []
    for fn in ALL:
        try:
            out.extend(fn(pack))
        except TableError as exc:
            out.append(F(fn.__name__, "error", "", 0, "unparseable table: %s" % exc))
    return out
