"""Self-tests for the pack scripts. Stdlib unittest only."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _mdtable
import _packmodel as pm
import adopt
import checks
import rotate

GOOD = "# T\n\n| id | status |\n|---|---|\n| a | done |\n"


class TestMdtable(unittest.TestCase):
    def test_parses_a_well_formed_table(self):
        t = _mdtable.find_tables(GOOD)[0]
        self.assertEqual(t.header, ["id", "status"])
        self.assertEqual(t.rows, [{"id": "a", "status": "done"}])

    def test_column_names_are_case_insensitive(self):
        text = "# T\n\n| Fact | Value | Verified by | Last verified |\n|---|---|---|---|\n| OS | Windows | AHMAD | 2026-01-01 |\n"
        t = _mdtable.find_tables(text)[0]
        self.assertEqual(t.header, ["Fact", "Value", "Verified by", "Last verified"])
        self.assertEqual(t.rows[0]["last verified"], "2026-01-01")
        self.assertEqual(t.col("Verified by"), ["AHMAD"])

    def test_ragged_row_is_an_error_not_a_silent_skip(self):
        bad = GOOD.replace("| a | done |", "| a | done | extra |")
        with self.assertRaises(_mdtable.TableError):
            _mdtable.find_tables(bad)

    def test_replace_table_round_trips(self):
        t = _mdtable.find_tables(GOOD)[0]
        out = _mdtable.replace_table(GOOD, t, [{"id": "b", "status": "pending"}])
        self.assertIn("| b | pending |", out)
        self.assertNotIn("| a | done |", out)
        self.assertTrue(out.startswith("# T"))

    def test_h2_blocks_split_in_file_order(self):
        blocks = _mdtable.h2_blocks("# H\n## One\na\n## Two\nb\n")
        self.assertEqual([t for t, _ in blocks], ["One", "Two"])
        self.assertEqual(blocks[0][1], "## One\na")

    def test_fenced_blocks_are_found(self):
        self.assertEqual(_mdtable.fenced_blocks("x\n" + chr(96) * 3 + "\nhi\n" + chr(96) * 3 + "\n"), [(2, "hi")])

    def test_an_escaped_pipe_is_not_a_delimiter(self):
        text = "# T\n\n| id | note |\n|---|---|\n| a | " + chr(96) + "x \\| y" + chr(96) + " |\n"
        t = _mdtable.find_tables(text)[0]
        self.assertEqual(t.rows[0]["note"], chr(96) + "x | y" + chr(96))

    def test_render_escapes_a_pipe_inside_a_cell(self):
        out = _mdtable.render(["id", "note"], [{"id": "a", "note": "x|y"}])
        self.assertEqual(out[2], "| a | x\\|y |")
        back = _mdtable.find_tables("# T\n\n" + "\n".join(out) + "\n")[0]
        self.assertEqual(back.rows[0]["note"], "x|y")

    def test_has_answers_for_column_names_ignoring_case(self):
        t = _mdtable.find_tables("# T\n\n| ID | Status |\n|---|---|\n| a | done |\n")[0]
        self.assertTrue(t.has("id"))
        self.assertTrue(t.has("id", "STATUS"))
        self.assertFalse(t.has("owner"))


class TestPackModel(unittest.TestCase):
    def test_a_timestamp_needs_an_offset(self):
        self.assertIsNone(pm.parse_ts("2026-10-13"))
        self.assertIsNone(pm.parse_ts("2026-10-13 10:00"))
        ts = pm.parse_ts("2026-10-13T10:00:00+03:00")
        self.assertIsNotNone(ts)
        self.assertEqual(int(ts.utcoffset().total_seconds()), 10800)

    def test_entries_split_on_h2_and_carry_a_stable_key(self):
        pack = pm.Pack(Path("."))
        text = (pm.BEGIN + "\n## 2026-10-13T10:00:00+03:00 First\nbody a\n"
                "## 2026-10-12T10:00:00+03:00 Second\nbody b\n" + pm.END + "\n")
        pack.read = lambda rel: text
        entries = pack.entries("docs/03-handoff.md")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0].key, "2026-10-13T10:00:00+03:00|2026-10-13T10:00:00+03:00 First")

    def test_no_markers_means_no_entries(self):
        pack = pm.Pack(Path("."))
        pack.read = lambda rel: "## 2026-10-13T10:00:00+03:00 Orphan\n"
        self.assertEqual(pack.entries("docs/03-handoff.md"), [])

    def test_scaffolding_entries_are_not_scoped_entries(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ("AGENTS.md", "app/AGENTS.md", "_dev/stage-b/target/AGENTS.md",
                        "skills/adopt-context-pack/AGENTS.md"):
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("# entry\n", encoding="utf-8")
            pack = pm.Pack(root)
            self.assertEqual(pack.scoped_agents(), ["app/AGENTS.md"])
            saved = pm.SKIP_TOP
            pm.SKIP_TOP = set()
            try:
                self.assertEqual(pack.scoped_agents(),
                                 ["_dev/stage-b/target/AGENTS.md", "app/AGENTS.md",
                                  "skills/adopt-context-pack/AGENTS.md"])
            finally:
                pm.SKIP_TOP = saved

    def test_size_counts_the_bytes_git_stores(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.md"
            f.write_bytes(b"one\r\ntwo\r\n")
            self.assertEqual(f.stat().st_size, 10)
            self.assertEqual(pm.Pack(Path(d)).size("a.md"), 8)


class TestContracts(unittest.TestCase):
    def test_only_pack_owned_docs_owe_a_contract(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ("docs/01-project.md", "docs/09-decisions/0001-x.md", "docs/architecture.md",
                        "docs/api/endpoints.md"):
                f = root / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("# no contract here\n", encoding="utf-8")
            found = sorted(f.path for f in checks.c00_contracts(pm.Pack(root)))
            self.assertEqual(found, ["docs/01-project.md", "docs/09-decisions/0001-x.md"])


class TestAdoptLite(unittest.TestCase):
    def test_lite_never_drops_a_protected_module(self):
        self.assertFalse(adopt.LITE_DROP & pm.PROTECTED_RULES)

    def test_lite_names_only_shipped_modules(self):
        self.assertLessEqual(set(adopt.LITE_DROP), set(adopt.shipped_rule_numbers()))


class TestHandoffLag(unittest.TestCase):
    def git(self, root, *args):
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], check=True, capture_output=True)

    def repo(self, root, extra_commits):
        self.git(root, "init", "-q")
        (root / "docs").mkdir()
        (root / "docs" / "03-handoff.md").write_text("# H\n", encoding="utf-8")
        self.git(root, "add", "-A")
        self.git(root, "commit", "-q", "-m", "handoff")
        for n in range(extra_commits):
            (root / ("f%d.txt" % n)).write_text("x", encoding="utf-8")
            self.git(root, "add", "-A")
            self.git(root, "commit", "-q", "-m", "work %d" % n)

    def test_no_git_repository_is_not_a_finding(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "docs").mkdir()
            (root / "docs" / "03-handoff.md").write_text("# H\n", encoding="utf-8")
            self.assertEqual(checks.c14_handoff_lag(pm.Pack(root)), [])

    def test_a_few_commits_after_the_handoff_are_normal(self):
        with tempfile.TemporaryDirectory() as d:
            self.repo(Path(d), checks.HANDOFF_LAG)
            self.assertEqual(checks.c14_handoff_lag(pm.Pack(Path(d))), [])

    def test_a_handoff_behind_the_history_warns(self):
        with tempfile.TemporaryDirectory() as d:
            self.repo(Path(d), checks.HANDOFF_LAG + 1)
            found = checks.c14_handoff_lag(pm.Pack(Path(d)))
            self.assertEqual([f.check for f in found], ["handoff-lag"])
            self.assertEqual(found[0].severity, "warning")

    def test_an_uncommitted_handoff_edit_is_a_session_writing_it(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.repo(root, checks.HANDOFF_LAG + 1)
            (root / "docs" / "03-handoff.md").write_text("# H\n\nnew entry\n", encoding="utf-8")
            self.assertEqual(checks.c14_handoff_lag(pm.Pack(root)), [])


class TestRotate(unittest.TestCase):
    def _pack(self, tmp, count, tasks=None):
        root = Path(tmp)
        (root / "docs" / "handoff").mkdir(parents=True)
        (root / "docs" / "tasks").mkdir(parents=True)
        entries = [
            "## 2026-10-%02dT10:00:00+03:00 Entry %d\n%s" % (i + 1, i + 1, "x" * 2000)
            for i in range(count)
        ]
        (root / "docs" / "03-handoff.md").write_text(
            "# Handoff\n\n" + pm.BEGIN + "\n" + "\n\n".join(entries) + "\n" + pm.END + "\n",
            encoding="utf-8",
        )
        (root / "docs" / "handoff" / "archive-2026.md").write_text(
            "# Handoff archive -- 2026\n", encoding="utf-8")
        if tasks is not None:
            (root / "docs" / "04-tasks.md").write_text(tasks, encoding="utf-8")
            (root / "docs" / "tasks" / "archive-2026.md").write_text(
                "# Tasks archive -- 2026\n", encoding="utf-8")
        return pm.Pack(root)

    @staticmethod
    def _tasks(cols, rows):
        head = "| " + " | ".join(cols) + " |"
        sep = "|" + "|".join("---" for _ in cols) + "|"
        body = "\n".join("| " + " | ".join(r) + " |" for r in rows)
        return "# 04 -- Tasks (live status)\n\n%s\n%s\n%s\n" % (head, sep, body)

    def test_the_oldest_entries_rotate_and_the_newest_stay(self):
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 7)
            res = rotate.run(pack, apply=True, year=2026)
            self.assertLessEqual(res[0]["inline_bytes"], pm.CAPS["docs/03-handoff.md"])
            inline = [e.title for e in pack.entries("docs/03-handoff.md")]
            self.assertEqual([t.split()[-1] for t in inline], ["7", "6", "5", "4"])
            archived = [t for t, _ in _mdtable.h2_blocks(pack.read("docs/handoff/archive-2026.md"))]
            self.assertEqual([t.split()[-1] for t in archived], ["3", "2", "1"])

    def test_a_missing_archive_is_created_with_its_header(self):
        # A new year has no archive file yet, and every other test seeds one, so this is
        # the only place the header-building branch runs: its title table is keyed by the
        # live path, and indexing it with the archive path raises a KeyError.
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 7)
            rel = "docs/03-handoff.md"
            archive = pack.path("docs/handoff/archive-2027.md")
            self.assertFalse(archive.exists())
            res = rotate.run(pack, apply=True, year=2027)
            self.assertTrue(archive.is_file())
            archived = [t for t, _ in _mdtable.h2_blocks(pack.read("docs/handoff/archive-2027.md"))]
            self.assertTrue(archived)
            inline = [e.title for e in pack.entries(rel)]
            for title in archived:
                self.assertNotIn(title, inline)
            got = next(r for r in res if r["target"] == rel)
            self.assertEqual(got["moved"], len(archived))
            self.assertLessEqual(pack.capped_size(rel), pm.CAPS[rel])

    def test_a_second_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 6)
            rotate.run(pack, apply=True, year=2026)
            before = pack.read("docs/handoff/archive-2026.md")
            res = rotate.run(pack, apply=True, year=2026)
            self.assertEqual(res[0]["moved"], 0)
            self.assertEqual(pack.read("docs/handoff/archive-2026.md"), before)

    def test_an_entry_already_in_the_archive_is_not_duplicated(self):
        # A re-merge can leave an archived entry inline again. The (timestamp, title) key
        # exists so it is not archived twice, but it must still leave the live region: the
        # point of the move is that the region comes back under its cap, and an entry that
        # stays inline while its copy already sits in the archive leaves --check red
        # forever. Counting moves, or counting archive copies, cannot see either half.
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 7)
            oldest = min(pack.entries("docs/03-handoff.md"), key=lambda e: e.ts)
            arch = pack.path("docs/handoff/archive-2026.md")
            arch.write_text(arch.read_text(encoding="utf-8") + "\n" + oldest.text + "\n",
                            encoding="utf-8")
            res = rotate.run(pack, apply=True, year=2026)
            archived = [t for t, _ in _mdtable.h2_blocks(pack.read("docs/handoff/archive-2026.md"))]
            inline = [e.title for e in pack.entries("docs/03-handoff.md")]
            self.assertEqual(res[0]["already_archived"], 1)
            self.assertEqual(archived.count(oldest.title), 1)
            self.assertNotIn(oldest.title, inline)
            self.assertLessEqual(res[0]["inline_bytes"], pm.CAPS["docs/03-handoff.md"])

    def test_every_planned_move_leaves_the_live_region_when_all_are_archived(self):
        # A re-merge can put entries the archive already holds back inline. The (timestamp,
        # title) key stops a second copy, but the inline rewrite must still happen: an entry
        # left in both homes keeps the region over its cap and --check red forever. The other
        # test seeds one of the planned moves, so `fresh` is non-empty and cannot see this:
        # every planned move has to be seeded for the case to exist at all.
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 7)
            rel = "docs/03-handoff.md"
            _, move = rotate.plan_blocks(pack, rel, pm.CAPS[rel], pm.NEWEST_ENTRIES)
            self.assertTrue(move)
            arch = pack.path("docs/handoff/archive-2026.md")
            arch.write_text(arch.read_text(encoding="utf-8") + "\n"
                            + "\n\n".join(e.text for e in move) + "\n", encoding="utf-8")
            res = rotate.run(pack, apply=True, year=2026)
            got = next(r for r in res if r["target"] == rel)
            inline = [e.title for e in pack.entries(rel)]
            archived = [t for t, _ in _mdtable.h2_blocks(pack.read("docs/handoff/archive-2026.md"))]
            self.assertEqual(got["already_archived"], len(move))
            self.assertEqual(got["moved"], 0)
            for e in move:
                self.assertNotIn(e.title, inline)
                self.assertEqual(archived.count(e.title), 1)
            self.assertLessEqual(pack.capped_size(rel), pm.CAPS[rel])

    def test_task_rows_rotate_into_the_tasks_archive(self):
        rows = [["t-1", "one", "done", "a", "docs", "-", "`e.md`"],
                ["t-2", "two", "pending", "b", "docs", "-", ""]]
        for cols in (["id", "title", "status", "owner", "scope", "gate", "evidence"],
                     ["ID", "Title", "Status", "Owner", "Scope", "Gate", "Evidence"]):
            with self.subTest(header=cols[0]):
                with tempfile.TemporaryDirectory() as d:
                    pack = self._pack(d, 1, tasks=self._tasks(cols, rows))
                    res = rotate.run(pack, apply=True, year=2026)
                    done = [r for r in res if r["target"] == "docs/04-tasks.md"][0]
                    self.assertEqual((done["moved"], done["kept"]), (1, 1))
                    archive = pack.read("docs/tasks/archive-2026.md")
                    self.assertIn("t-1", archive)
                    self.assertNotIn("t-2", archive)
                    self.assertNotIn("t-1", pack.read("docs/04-tasks.md"))

    def test_a_bad_tasks_table_leaves_the_whole_tree_unchanged(self):
        # Planning and writing must not interleave: a tasks table this bad raises Abort,
        # and by then the handoff above it has already been rotated in place.
        with tempfile.TemporaryDirectory() as d:
            ragged = self._tasks(["id", "title", "status"],
                                 [["t-1", "one", "done"], ["t-2", "two"]])
            pack = self._pack(d, 7, tasks=ragged)
            rels = ("docs/03-handoff.md", "docs/handoff/archive-2026.md",
                    "docs/04-tasks.md", "docs/tasks/archive-2026.md")
            before = {rel: pack.read(rel) for rel in rels}
            with self.assertRaises(rotate.Abort):
                rotate.run(pack, apply=True, year=2026)
            self.assertEqual({rel: pack.read(rel) for rel in rels}, before)

    def test_a_timestampless_entry_aborts_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 6)
            text = pack.read("docs/03-handoff.md").replace("2026-10-01T10:00:00+03:00", "no timestamp")
            pack.path("docs/03-handoff.md").write_text(text, encoding="utf-8")
            with self.assertRaises(rotate.Abort):
                rotate.run(pack, apply=True, year=2026)
            self.assertIn("no timestamp", pack.read("docs/03-handoff.md"))

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            pack = self._pack(d, 6)
            before = pack.read("docs/03-handoff.md")
            rotate.run(pack, apply=False, year=2026)
            self.assertEqual(pack.read("docs/03-handoff.md"), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
