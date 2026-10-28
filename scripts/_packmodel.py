"""Parse a context pack directory into a queryable model. Stdlib only."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from _mdtable import Table, find_tables, h2_blocks

BEGIN = "<!-- rotate:begin -->"
END = "<!-- rotate:end -->"

TS = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:?\d{2})")
RULE_ID = re.compile(r"\bR-(\d{2})\.(\d+)\b")
AGENTS_TITLE = re.compile(r'AGENTS\.md,\s*"([^"]+)"')

PROTECTED_RULES = frozenset({"03", "04", "08", "11", "12", "16"})

CAPS = {
    "SOUL.md": 4096,
    "AGENTS.md": 12288,
    "docs/03-handoff.md": 8192,
    "docs/04-tasks.md": 4096,
    "docs/08-coordination.md": 2048,
}
# Three of these caps apply to the bytes between the rotation markers, not to the
# whole file. docs/08-coordination.md is multi-KB as a file while its claims region
# is under a kilobyte, so a whole-file measure reports a false over-cap
# and the "fix" that follows from it is a real content cut to satisfy a budget the
# file never exceeded.
REGION_CAPPED = ("docs/03-handoff.md", "docs/04-tasks.md", "docs/08-coordination.md")

# What ships. _dev/ holds this pack's own design record and plan, .superpowers/ holds
# scratch, and skills/ holds the adoption skill this repository distributes; none is
# copied into a target and none is any adopter's business. Defined here once, because
# three tools need the same answer: adopt.py skips these when copying, and the ADAPT
# report and the rule-id check skip them so their numbers and findings describe the
# pack rather than the plan that built it or the prose that describes it.
# .claude/ is a harness's own local state (settings, saved transcripts); a saved
# transcript quotes the pack, so scanning it reports every quote as a duplicate fact.
SKIP_TOP = {"_dev", ".git", ".superpowers", "__pycache__", ".venv", ".locks", "skills", ".claude"}
ADAPTER_CAP = 1024
SCOPED_CAP = 6144
ENTRY_CAP = 2048
NEWEST_ENTRIES = 3
BOOT_TOTAL = 36864

ADAPTERS = ("CLAUDE.md", "GEMINI.md", ".cursor/rules/pack.mdc", ".github/copilot-instructions.md")

REQUIRED = (
    "SOUL.md", "AGENTS.md", "rules/00-index.md", "docs/00-schema.md",
    "docs/01-project.md", "docs/02-environment.md", "docs/03-handoff.md",
    "docs/04-tasks.md", "docs/05-memory.md", "docs/06-glossary.md",
    "docs/07-sources-of-truth.md", "docs/08-coordination.md",
)


def parse_ts(text: str) -> datetime | None:
    """ISO-8601 with an explicit offset, else None. A bare date is not a timestamp."""
    m = TS.search(text or "")
    if not m:
        return None
    date, clock, off = m.groups()
    if off == "Z":
        off = "+00:00"
    elif re.fullmatch(r"[+-]\d{4}", off):
        off = off[:3] + ":" + off[3:]
    try:
        return datetime.fromisoformat("%sT%s%s" % (date, clock, off))
    except ValueError:
        return None


def rule_ids(text: str) -> list[str]:
    return ["R-%s.%s" % m for m in RULE_ID.findall(text or "")]


@dataclass
class Entry:
    key: str
    title: str
    ts: datetime | None
    text: str


@dataclass
class Pack:
    root: Path

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return (self.root / rel).is_file()

    def read(self, rel: str) -> str:
        p = self.root / rel
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def size(self, rel: str) -> int:
        """Bytes as a cap must count them: UTF-8 length with LF endings.

        The adapter and scoped caps are written in bytes, and st_size is not those
        bytes: read_text turns a CRLF pair into one LF, so on a Windows checkout with
        core.autocrlf=true the same file measures two bytes per line larger than it
        does in CI, and a cap that passes here fails there. capped_size already
        counted this way; size now agrees with it."""
        return len(self.read(rel).encode("utf-8"))

    def tables(self, rel: str) -> list[Table]:
        return find_tables(self.read(rel))

    def rule_modules(self) -> list[tuple[str, str]]:
        out = []
        for p in sorted((self.root / "rules").glob("*.md")):
            m = re.match(r"^(\d{2})-", p.name)
            if m:
                out.append((m.group(1), p.name))
        return out

    def scoped_agents(self) -> list[str]:
        out = []
        for p in sorted(self.root.rglob("AGENTS.md")):
            if p == self.root / "AGENTS.md":
                continue
            rel = str(p.relative_to(self.root)).replace(chr(92), "/")
            # Build scaffolding is not part of the pack; every other walker in this
            # file and in checks.py drops SKIP_TOP, and a fixture committed under
            # _dev/ is exactly the case that caught this one not doing it.
            if set(rel.split("/")[:-1]) & SKIP_TOP:
                continue
            out.append(rel)
        return sorted(out)

    def component_rows(self) -> list[dict[str, str]]:
        for t in self.tables("AGENTS.md"):
            if t.has("id"):
                return t.rows
        return []

    def region(self, rel: str) -> str:
        text = self.read(rel)
        if BEGIN not in text or END not in text:
            return ""
        return text.split(BEGIN, 1)[1].split(END, 1)[0]

    def capped_size(self, rel: str) -> int:
        """The bytes a budget counts: the rotation region for a region-capped
        file, the whole file otherwise. Every cap check uses this, so no check
        can silently measure something other than what the cap is written about."""
        text = self.region(rel) if rel in REGION_CAPPED else self.read(rel)
        return len(text.encode("utf-8"))

    def entries(self, rel: str) -> list[Entry]:
        out: list[Entry] = []
        for title, body in h2_blocks(self.region(rel)):
            ts = parse_ts(title)
            out.append(Entry(key="%s|%s" % (ts.isoformat() if ts else "", title),
                             title=title, ts=ts, text=body))
        return out
