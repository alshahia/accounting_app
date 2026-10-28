"""Strict Markdown table and heading parsing. Stdlib only."""
from __future__ import annotations

import re
from dataclasses import dataclass

SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
CELL = re.compile(r"(?<!\\)\|")


class TableError(ValueError):
    """A table is malformed. Callers report this; they never skip it silently."""


def cells(line: str) -> list[str]:
    """Split one row. A backslash-escaped pipe is a cell's content, not a delimiter.

    docs/00-schema.md writes a vocabulary cell as several escaped pipes, which is five
    pipe characters and one cell. Splitting on every pipe read that row as five cells
    against a two-cell header and raised, so the one file that defines every vocabulary
    was the one file no table could be read from."""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in CELL.split(s)]


@dataclass(frozen=True)
class Table:
    """start/end index text.split(chr(10)); start is the header line, end is exclusive."""

    header: list[str]
    rows: list[dict[str, str]]
    start: int
    end: int

    def line(self) -> int:
        return self.start + 1

    def col(self, name: str) -> list[str]:
        return [r.get(name.lower(), "") for r in self.rows]

    def has(self, *names: str) -> bool:
        """True when the header carries every named column, ignoring case.

        One home for the question "is this the table I mean": a caller asks for the
        columns it needs instead of comparing header spellings, which differ per file
        (docs/04-tasks.md is lowercase, docs/02-environment.md is title-case)."""
        got = {h.lower() for h in self.header}
        return all(n.lower() in got for n in names)


def find_tables(text: str) -> list[Table]:
    lines = text.split("\n")
    out: list[Table] = []
    i = 0
    while i < len(lines) - 1:
        if "|" in lines[i] and SEPARATOR.match(lines[i + 1] or ""):
            header = cells(lines[i])
            j = i + 2
            rows: list[dict[str, str]] = []
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                got = cells(lines[j])
                if len(got) != len(header):
                    raise TableError("line %d: %d cells, header has %d" % (j + 1, len(got), len(header)))
                # Row keys are lowercased, while `header` keeps the file's own rendering
                # because `render` writes the header back. A column name is a name, not a
                # spelling: docs/00-schema.md declares `Last verified` and `Verified by` for
                # the environment table and lowercase names for the task and claim tables, and
                # every lookup in the checks is written lowercase. Comparing literally made c05
                # and c06 read the environment table as absent: c05 checked no timestamp at all,
                # and c06 warned that no environment table with those two columns existed.
                rows.append({k.lower(): v for k, v in zip(header, got)})
                j += 1
            out.append(Table(header, rows, i, j))
            i = j
        else:
            i += 1
    return out


def render(header: list[str], rows: list[dict[str, str]]) -> list[str]:
    # A cell value may hold a pipe (find_tables unescapes them), so it goes back
    # escaped: writing the raw character turns one cell into two and breaks the table.
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(r.get(h.lower(), "").replace("|", "\\|") for h in header) + " |")
    return out


def replace_table(text: str, table: Table, rows: list[dict[str, str]]) -> str:
    lines = text.split("\n")
    return "\n".join(lines[: table.start] + render(table.header, rows) + lines[table.end :])


def h2_blocks(text: str) -> list[tuple[str, str]]:
    """Return (title, body) for each H2 block, in file order."""
    lines = text.split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("## ")]
    out: list[tuple[str, str]] = []
    for n, i in enumerate(starts):
        j = starts[n + 1] if n + 1 < len(starts) else len(lines)
        out.append((lines[i][3:].strip(), "\n".join(lines[i:j]).strip()))
    return out


def fenced_blocks(text: str) -> list[tuple[int, str]]:
    """Return (1-based start line, content) for each fenced block."""
    lines = text.split("\n")
    out: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith(chr(96) * 3):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(chr(96) * 3):
                j += 1
            out.append((i + 1, "\n".join(lines[i + 1 : j])))
            i = j + 1
        else:
            i += 1
    return out
