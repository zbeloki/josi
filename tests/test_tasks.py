"""End-to-end tests for actl Stage 2 task management.

Every test runs the CLI as a subprocess, so data persistence between
invocations is exercised implicitly. The Stage 1 fixtures (temporary Git
repository, fake opencode executable, environment) come from ``test_actl``.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import unittest
from pathlib import Path

from test_actl import MINIMAL_PATH, ActlTestCase


@unittest.skipUnless(shutil.which("git"), "git is required")
class TaskTestCase(ActlTestCase):
    """Base fixture: an initialized project with a fake opencode on PATH."""

    def setUp(self):
        super().setUp()
        self.initialize()

    # -- helpers ---------------------------------------------------------

    def task_db(self) -> Path:
        return self.repo / ".actl" / "tasks.db"

    def create_task(self, *args):
        return self.run_actl("task", "create", *args)

    def created_id(self, result) -> str:
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("Created task t"), result.stdout)
        return result.stdout.split()[-1]

    def show(self, task_id):
        return self.run_actl("task", "show", str(task_id))

    def make_tasks(self, *titles):
        ids = []
        for title in titles:
            ids.append(self.created_id(self.create_task("--title", title)))
        return ids


class TaskCrudTests(TaskTestCase):
    def test_create_list_and_show(self):
        self.assertEqual(self.created_id(self.create_task(
            "--title", "Write tests", "--description", "Cover the CLI",
        )), "t1")
        self.assertEqual(self.created_id(self.create_task("--title", "Ship it")), "t2")
        self.assertTrue(self.task_db().is_file())

        listing = self.run_actl("task", "list")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        lines = listing.stdout.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("t1", lines[0])
        self.assertIn("PENDING", lines[0])
        self.assertIn("Write tests", lines[0])
        self.assertIn("t2", lines[1])
        self.assertIn("Ship it", lines[1])

        shown = self.show("t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("id: t1", shown.stdout)
        self.assertIn("title: Write tests", shown.stdout)
        self.assertIn("status: PENDING", shown.stdout)
        self.assertIn("created_at: ", shown.stdout)
        self.assertIn("updated_at: ", shown.stdout)
        self.assertIn("depends_on: (none)", shown.stdout)
        self.assertIn("description:\nCover the CLI", shown.stdout)

        empty = self.show("t2")
        self.assertEqual(empty.returncode, 0, empty.stderr)
        self.assertIn("description:\n(none)", empty.stdout)

    def test_empty_task_list(self):
        result = self.run_actl("task", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_show_accepts_t_prefix_and_bare_integer(self):
        self.make_tasks("One")
        for form in ("1", "t1", "T1"):
            result = self.show(form)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("title: One", result.stdout)

    def test_update_title_and_description(self):
        self.make_tasks("Old title")
        self.run_actl("task", "update", "t1", "--description", "old body")
        before = self.show("t1").stdout

        result = self.run_actl(
            "task", "update", "t1", "--title", "New title",
            "--description", "new body",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated task t1", result.stdout)

        after = self.show("t1").stdout
        self.assertIn("title: New title", after)
        self.assertIn("description:\nnew body", after)
        created_at = self._field(before, "created_at")
        self.assertEqual(self._field(after, "created_at"), created_at)
        self.assertNotEqual(self._field(after, "updated_at"), self._field(before, "updated_at"))

    def test_update_requires_a_change(self):
        self.make_tasks("One")
        result = self.run_actl("task", "update", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no changes", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_create_rejects_blank_title(self):
        result = self.create_task("--title", "   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be empty", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.run_actl("task", "list").stdout, "")

    def test_update_rejects_blank_title(self):
        self.make_tasks("One")
        result = self.run_actl("task", "update", "t1", "--title", "   ")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be empty", result.stderr)
        self.assertIn("title: One", self.show("t1").stdout)

    def test_description_file(self):
        (self.repo / "description.txt").write_text(
            "First line\nSecond line\n", encoding="utf-8"
        )
        result = self.create_task(
            "--title", "Long", "--description-file", "description.txt"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        shown = self.show("t1")
        self.assertIn("description:\nFirst line\nSecond line\n", shown.stdout)

        (self.repo / "updated.txt").write_text("Replaced\n", encoding="utf-8")
        result = self.run_actl(
            "task", "update", "t1", "--description-file", "updated.txt"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("description:\nReplaced\n", self.show("t1").stdout)

    def test_description_flags_are_mutually_exclusive(self):
        (self.repo / "description.txt").write_text("x", encoding="utf-8")
        result = self.create_task(
            "--title", "T", "--description", "a",
            "--description-file", "description.txt",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with", result.stderr)

    def test_missing_description_file(self):
        result = self.create_task("--title", "T", "--description-file", "missing.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read description file", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_undecodable_description_file(self):
        (self.repo / "bad.txt").write_bytes(b"\xff\xfe")
        result = self.create_task("--title", "T", "--description-file", "bad.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_delete_task(self):
        self.make_tasks("Gone")
        result = self.run_actl("task", "delete", "t1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Deleted task t1", result.stdout)
        self.assertNotEqual(self.show("t1").returncode, 0)
        self.assertEqual(self.run_actl("task", "list").stdout, "")

    def test_unknown_tasks_are_reported(self):
        cases = (
            ("task", "show", "t5"),
            ("task", "update", "t5", "--title", "x"),
            ("task", "delete", "t5"),
        )
        for args in cases:
            with self.subTest(args=args):
                result = self.run_actl(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("task t5 does not exist", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_invalid_task_ids_are_rejected(self):
        for value in ("0", "t0", "banana", "t1x", "1.5", "-1", ""):
            with self.subTest(value=value):
                result = self.show(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid task id", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_database_stores_integer_ids_and_pending_status(self):
        self.make_tasks("One")
        conn = sqlite3.connect(str(self.task_db()))
        try:
            rows = conn.execute("SELECT id, status, title FROM tasks").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(1, "PENDING", "One")])

    def _field(self, output, name):
        for line in output.splitlines():
            if line.startswith(name + ": "):
                return line
        self.fail(name + " not found in:\n" + output)


class TaskDependencyTests(TaskTestCase):
    def test_multiple_dependencies(self):
        self.make_tasks("One", "Two")
        result = self.create_task(
            "--title", "Three", "--depends-on", "t1", "--depends-on", "t2"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("depends_on: t1, t2", self.show("t3").stdout)

    def test_comma_separated_dependencies(self):
        self.make_tasks("One", "Two")
        result = self.create_task("--title", "Three", "--depends-on", "1,t2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("depends_on: t1, t2", self.show("t3").stdout)

    def test_duplicate_dependencies_are_stored_once(self):
        self.make_tasks("One")
        result = self.create_task(
            "--title", "Two", "--depends-on", "t1", "--depends-on", "t1,t1"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("depends_on: t1", self.show("t2").stdout)
        self.assertNotIn("depends_on: t1, t1", self.show("t2").stdout)

    def test_self_dependency_is_rejected(self):
        self.make_tasks("One")
        result = self.run_actl("task", "update", "t1", "--depends-on", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot depend on itself", result.stderr)
        self.assertIn("depends_on: (none)", self.show("t1").stdout)

    def test_dependency_on_missing_task_is_rejected(self):
        self.make_tasks("One")
        cases = (
            ("task", "create", "--title", "Two", "--depends-on", "t9"),
            ("task", "update", "t1", "--depends-on", "t9"),
        )
        for args in cases:
            with self.subTest(args=args):
                result = self.run_actl(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("task t9 does not exist", result.stderr)
        # The failed create must not have stored a task.
        self.assertEqual(len(self.run_actl("task", "list").stdout.splitlines()), 1)

    def test_direct_cycle_is_rejected(self):
        self.make_tasks("One", "Two")
        self.run_actl("task", "update", "t2", "--depends-on", "t1")

        result = self.run_actl("task", "update", "t1", "--depends-on", "t2")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cycle", result.stderr)
        self.assertIn("depends_on: (none)", self.show("t1").stdout)
        self.assertIn("depends_on: t1", self.show("t2").stdout)

    def test_indirect_cycle_is_rejected(self):
        self.make_tasks("One", "Two", "Three")
        self.run_actl("task", "update", "t2", "--depends-on", "t1")
        self.run_actl("task", "update", "t3", "--depends-on", "t2")

        for task_id, dependency in (("t1", "t3"), ("t1", "t2"), ("t2", "t3")):
            with self.subTest(task=task_id, dependency=dependency):
                result = self.run_actl(
                    "task", "update", task_id, "--depends-on", dependency
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cycle", result.stderr)
        self.assertIn("depends_on: (none)", self.show("t1").stdout)

    def test_update_replaces_dependencies(self):
        self.make_tasks("One", "Two", "Three")
        self.run_actl("task", "update", "t3", "--depends-on", "t1", "--depends-on", "t2")
        result = self.run_actl("task", "update", "t3", "--depends-on", "t2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("depends_on: t2\n", self.show("t3").stdout)

    def test_clear_dependencies(self):
        self.make_tasks("One", "Two")
        self.run_actl("task", "update", "t2", "--depends-on", "t1")
        result = self.run_actl("task", "update", "t2", "--clear-dependencies")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("depends_on: (none)", self.show("t2").stdout)
        self.assertEqual(self.run_actl("task", "delete", "t1").returncode, 0)

    def test_depends_on_and_clear_dependencies_conflict(self):
        self.make_tasks("One", "Two")
        result = self.run_actl(
            "task", "update", "t2", "--depends-on", "t1", "--clear-dependencies"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not allowed with", result.stderr)

    def test_delete_with_dependents_is_refused(self):
        self.make_tasks("One", "Two", "Three")
        self.run_actl("task", "update", "t2", "--depends-on", "t1")
        self.run_actl("task", "update", "t3", "--depends-on", "t1")

        result = self.run_actl("task", "delete", "t1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot delete task t1", result.stderr)
        self.assertIn("t2, t3 depend on it", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.show("t1").returncode, 0)

        self.assertEqual(self.run_actl("task", "delete", "t2").returncode, 0)
        self.assertEqual(self.run_actl("task", "delete", "t3").returncode, 0)
        self.assertEqual(self.run_actl("task", "delete", "t1").returncode, 0)

    def test_delete_removes_its_own_dependency_rows(self):
        self.make_tasks("One", "Two")
        self.run_actl("task", "update", "t2", "--depends-on", "t1")
        self.assertEqual(self.run_actl("task", "delete", "t2").returncode, 0)

        conn = sqlite3.connect(str(self.task_db()))
        try:
            rows = conn.execute("SELECT * FROM dependencies").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [])


@unittest.skipUnless(shutil.which("git"), "git is required")
class TaskProjectTests(ActlTestCase):
    """Task commands require a project and never cross project boundaries."""

    def test_init_creates_the_database(self):
        self.initialize()
        self.assertTrue((self.repo / ".actl" / "tasks.db").is_file())

    def test_task_commands_require_initialized_project(self):
        self.init_git()
        cases = (
            ("task", "list"),
            ("task", "create", "--title", "x"),
            ("task", "show", "t1"),
            ("task", "update", "t1", "--title", "y"),
            ("task", "delete", "t1"),
        )
        for args in cases:
            with self.subTest(args=args):
                result = self.run_actl(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("actl init", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse((self.repo / ".actl").exists())
        self.assertEqual(self.calls(), [])

    def test_task_commands_from_subdirectory_use_the_project_root(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "Root task").returncode, 0
        )
        sub = self.repo / "src" / "nested"
        sub.mkdir(parents=True)
        result = self.run_actl("task", "list", cwd=sub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Root task", result.stdout)
        self.assertTrue((self.repo / ".actl" / "tasks.db").is_file())
        self.assertFalse((sub / ".actl").exists())

    def test_task_commands_outside_git_repository(self):
        outside = self.base / "outside"
        outside.mkdir()
        result = self.run_actl("task", "list", cwd=outside)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)

    def test_task_commands_ignore_actl_outside_git(self):
        self.initialize()
        self.run_actl("task", "create", "--title", "Parent task")
        copied = self.base / "copied"
        shutil.copytree(self.repo / ".actl", copied / ".actl")
        result = self.run_actl("task", "list", cwd=copied)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git repository", result.stderr)

    def test_task_commands_with_invalid_metadata(self):
        self.init_git()
        (self.repo / ".actl").mkdir()
        (self.repo / ".actl" / "project.json").write_text("not json", encoding="utf-8")
        result = self.run_actl("task", "list")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid actl project metadata", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_stage1_project_gets_a_database_on_first_task_command(self):
        self.initialize()
        db = self.repo / ".actl" / "tasks.db"
        db.unlink()
        self.assertFalse(db.exists())

        result = self.run_actl("task", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(db.is_file())

        created = self.run_actl("task", "create", "--title", "One")
        self.assertEqual(created.returncode, 0, created.stderr)

    def test_repeated_initialization_preserves_tasks(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "Keep me").returncode, 0
        )

        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)

        shown = self.run_actl("task", "show", "t1")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertIn("title: Keep me", shown.stdout)

    def test_repeated_task_commands_preserve_tasks(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "Keep me").returncode, 0
        )
        for _ in range(3):
            self.assertEqual(self.run_actl("task", "list").returncode, 0)
        self.assertIn("title: Keep me", self.run_actl("task", "show", "t1").stdout)

    def test_projects_have_isolated_databases(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "Only in repo").returncode, 0
        )

        other = self.base / "other"
        other.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(other)], check=True, capture_output=True
        )
        self.assertEqual(self.run_actl("init", cwd=other).returncode, 0)
        listing = self.run_actl("task", "list", cwd=other)
        self.assertEqual(listing.returncode, 0, listing.stderr)
        self.assertEqual(listing.stdout, "")
        created = self.run_actl("task", "create", "--title", "Only in other", cwd=other)
        self.assertEqual(created.returncode, 0, created.stderr)

        listing = self.run_actl("task", "list")
        self.assertIn("Only in repo", listing.stdout)
        self.assertNotIn("Only in other", listing.stdout)

    def test_nested_repository_never_uses_the_parent_database(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "Parent task").returncode, 0
        )
        child = self.repo / "child-repo"
        child.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(child)], check=True, capture_output=True
        )
        result = self.run_actl("task", "list", cwd=child)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init", result.stderr)
        self.assertNotIn("Parent task", result.stdout + result.stderr)

    def test_task_commands_do_not_need_opencode(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "One").returncode, 0
        )
        if not shutil.which("git", path=MINIMAL_PATH):
            self.skipTest("git is not available on the minimal PATH")
        self.env["PATH"] = MINIMAL_PATH
        result = self.run_actl("task", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("One", result.stdout)

    def test_task_commands_leave_the_planner_alone(self):
        self.initialize()
        self.assertEqual(
            self.run_actl("task", "create", "--title", "One").returncode, 0
        )
        self.assertEqual(self.run_actl("task", "list").returncode, 0)
        self.assertEqual(self.run_actl("task", "show", "t1").returncode, 0)
        self.assertEqual(self.calls(), [], "task commands must not invoke OpenCode")
        self.assertFalse((self.repo / ".actl" / "planner.json").exists())
        self.assertFalse((self.repo / ".opencode").exists())

    def test_corrupt_database_is_reported_and_left_alone(self):
        self.initialize()
        db = self.repo / ".actl" / "tasks.db"
        db.write_bytes(b"this is not a sqlite database")
        result = self.run_actl("task", "list")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task database", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(db.read_bytes(), b"this is not a sqlite database")

    def test_schema_declares_foreign_keys(self):
        self.initialize()
        conn = sqlite3.connect(str(self.repo / ".actl" / "tasks.db"))
        try:
            foreign_keys = conn.execute("PRAGMA foreign_key_list(dependencies)").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(foreign_keys), 2)
        self.assertEqual({row[2] for row in foreign_keys}, {"tasks"})


if __name__ == "__main__":
    unittest.main()
