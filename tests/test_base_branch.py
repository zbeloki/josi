"""End-to-end tests for base-branch selection during ``actl init``.

Task branches are always created from the project's configured base branch.
These tests cover how that branch is chosen: an explicit ``--base``, the
remote default (``origin/HEAD``), the single branch among ``main`` and
``master``, the unborn ``HEAD`` of a repository without commits, and the
interactive question when the choice is ambiguous.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from test_actl import ActlTestCase


@unittest.skipUnless(shutil.which("git"), "git is required")
class BaseBranchInitTests(ActlTestCase):
    def git(self, *args, cwd=None, check=True):
        return subprocess.run(
            ["git", *args], cwd=str(cwd or self.repo), env=self.env,
            capture_output=True, text=True, check=check,
        )

    def commit(self, message="base commit", cwd=None):
        path = cwd or self.repo
        (path / "file.txt").write_text(message + "\n", encoding="utf-8")
        self.git("add", "-A", cwd=path)
        self.git(
            "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-m", message, cwd=path,
        )

    def start_repo(self, branch):
        self.init_git()
        self.git("symbolic-ref", "HEAD", "refs/heads/" + branch)
        self.commit()

    def metadata(self):
        return json.loads(self.metadata_path().read_text(encoding="utf-8"))

    def make_remote_with_default(self, default):
        """Point a local ``origin`` at ``default`` as its default branch."""
        remote = self.base / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(remote)],
            check=True, capture_output=True,
        )
        self.init_git()
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.commit("main commit")
        pushed = ["main"]
        if default != "main":
            self.git("checkout", "-q", "-b", default)
            self.commit(default + " commit")
            pushed.append(default)
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-q", "origin", *pushed)
        self.git(
            "-C", str(remote), "symbolic-ref", "HEAD", "refs/heads/" + default
        )
        self.git("remote", "set-head", "origin", "-a")

    # -- detection -------------------------------------------------------

    def test_init_detects_main(self):
        self.start_repo("main")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "main")
        self.assertIn("Base branch: main", result.stdout)

    def test_init_detects_master(self):
        self.start_repo("master")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "master")

    def test_init_uses_main_when_feature_branches_exist(self):
        self.start_repo("main")
        self.git("checkout", "-q", "-b", "feature/login")
        self.commit("feature commit")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "main")
        self.assertNotIn("could not be determined", result.stdout)

    def test_init_selects_the_only_existing_branch(self):
        self.start_repo("topic")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "topic")

    def test_init_ignores_an_unborn_checkout_when_branches_exist(self):
        # A repository is only empty when nothing was ever committed; an
        # orphan checkout must not hide the existing branches.
        self.start_repo("main")
        self.git("checkout", "-q", "--orphan", "scratch")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "main")

    def test_init_ignores_a_tag_that_shadows_a_branch_name(self):
        # The listed names must be real branch names, not unambiguous
        # abbreviations such as "heads/main".
        self.start_repo("master")
        self.git("branch", "main")
        self.git("tag", "main")
        result = self.run_actl("init", input_text="1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "main")

    def test_init_prompts_when_main_and_master_are_ambiguous(self):
        self.start_repo("master")
        self.git("branch", "main")
        # HEAD is on master, but the user picks the first candidate (main):
        # the checked-out branch must not win by assumption.
        result = self.run_actl("init", input_text="1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not be determined", result.stdout)
        self.assertIn("main", result.stdout)
        self.assertIn("master", result.stdout)
        self.assertEqual(self.metadata()["base_branch"], "main")

    def test_init_prompt_accepts_a_branch_name(self):
        self.start_repo("main")
        self.git("branch", "master")
        result = self.run_actl("init", input_text="master\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "master")

    def test_init_ambiguous_prompt_fails_cleanly_without_input(self):
        self.start_repo("master")
        self.git("branch", "main")
        result = self.run_actl("init")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("actl init --base", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.metadata_path().exists())

    # -- explicit --base -------------------------------------------------

    def test_explicit_base_is_stored(self):
        self.start_repo("main")
        self.git("branch", "develop")
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "develop")
        self.assertIn("Base branch: develop", result.stdout)

    def test_explicit_base_must_exist(self):
        self.start_repo("main")
        result = self.run_actl("init", "--base", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.metadata_path().exists())

    def test_explicit_base_must_exist_in_an_unborn_checkout(self):
        # An orphan checkout in a non-empty repository is not an empty
        # repository, so --base must still name an existing branch.
        self.start_repo("main")
        self.git("checkout", "-q", "--orphan", "scratch")
        result = self.run_actl("init", "--base", "missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse(self.metadata_path().exists())

    def test_explicit_base_rejects_invalid_branch_names(self):
        self.start_repo("main")
        for value in ("", "bad name", "a..b", "-oops", "@{-1}", "x~1"):
            with self.subTest(value=value):
                result = self.run_actl("init", "--base=" + value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("invalid base branch", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.metadata_path().exists())

    def test_explicit_base_beats_the_remote_default(self):
        self.make_remote_with_default("trunk")
        self.git("checkout", "-q", "main")
        result = self.run_actl("init", "--base", "main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "main")

    # -- remote default branch -------------------------------------------

    def test_remote_default_branch_is_used(self):
        self.make_remote_with_default("trunk")
        self.git("checkout", "-q", "main")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "trunk")
        self.assertIn("Base branch: trunk", result.stdout)

    def test_remote_default_is_ignored_without_a_local_branch(self):
        self.make_remote_with_default("trunk")
        self.git("checkout", "-q", "main")
        self.git("branch", "-D", "trunk")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "main")

    # -- repositories without commits ------------------------------------

    def test_empty_repository_uses_the_unborn_head(self):
        self.init_git()
        unborn = self.git("symbolic-ref", "--short", "HEAD").stdout.strip()
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], unborn)
        self.assertIn("Base branch: " + unborn, result.stdout)
        # init must never create a commit.
        self.assertNotEqual(
            self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0
        )

    def test_empty_repository_explicit_base_is_accepted_and_preserved(self):
        self.init_git()
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.metadata()["base_branch"], "develop")
        # The intended branch has no commits yet; a plain re-init preserves it.
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata()["base_branch"], "develop")

    # -- existing projects -----------------------------------------------

    def test_reinit_without_base_preserves_the_selection(self):
        self.start_repo("main")
        self.assertEqual(self.run_actl("init").returncode, 0)
        before = self.metadata_path().read_bytes()
        # ``main`` plus ``master`` is ambiguous, but the stored selection wins
        # and a plain init never changes it.
        self.git("branch", "master")
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already initialized", result.stdout)
        self.assertEqual(self.metadata_path().read_bytes(), before)

    def test_explicit_base_updates_an_existing_selection(self):
        self.start_repo("main")
        self.git("branch", "develop")
        self.assertEqual(self.run_actl("init").returncode, 0)
        result = self.run_actl("init", "--base", "develop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the base branch to develop", result.stdout)
        self.assertEqual(self.metadata()["base_branch"], "develop")

    def test_legacy_project_gains_a_base_branch_on_reinit(self):
        self.start_repo("main")
        self.assertEqual(self.run_actl("init").returncode, 0)
        metadata = self.metadata()
        del metadata["base_branch"]
        self.metadata_path().write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        result = self.run_actl("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Updated the base branch to main", result.stdout)
        updated = self.metadata()
        self.assertEqual(updated["base_branch"], "main")
        self.assertEqual(updated["created_at"], metadata["created_at"])


if __name__ == "__main__":
    unittest.main()
