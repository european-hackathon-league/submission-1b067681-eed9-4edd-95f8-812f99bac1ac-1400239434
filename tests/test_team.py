"""scripts/team.py against a throwaway repository with its own 'origin': several developers working at once."""
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("team", ROOT / "scripts" / "team.py")
team = importlib.util.module_from_spec(spec)
spec.loader.exec_module(team)

PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_broken():\n    assert False\n"


def sh(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def commit(repo, path, text, message):
    file = Path(repo) / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)
    sh("git", "add", path, cwd=repo)
    sh("git", "commit", "-q", "-m", message, cwd=repo)


class Team(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin, self.repo = base / "origin.git", base / "work"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.origin), cwd=base)
        sh("git", "clone", "-q", str(self.origin), str(self.repo), cwd=base)
        for key, value in (("user.email", "dev@example.invalid"), ("user.name", "Dev")):
            sh("git", "config", key, value, cwd=self.repo)
        (self.repo / ".gitignore").write_text(".integration/\n")
        commit(self.repo, "tests/test_ok.py", PASSING, "tests")
        commit(self.repo, "shared.py", "VALUE = 1\n", "base")
        sh("git", "push", "-q", "origin", "HEAD:main", cwd=self.repo)
        self.cwd = os.getcwd()
        os.chdir(self.repo)

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def branch(self, name, path, text):
        sh("git", "checkout", "-q", "-b", name, "origin/main", cwd=self.repo)
        commit(self.repo, path, text, f"{name}: {path}")
        sh("git", "push", "-q", "origin", name, cwd=self.repo)
        sh("git", "checkout", "-q", "--detach", "origin/main", cwd=self.repo)

    def advance_main(self, path, text):
        sh("git", "checkout", "-q", "-B", "main", "origin/main", cwd=self.repo)
        commit(self.repo, path, text, f"main: {path}")
        sh("git", "push", "-q", "origin", "main", cwd=self.repo)
        sh("git", "fetch", "-q", "origin", cwd=self.repo)

    def head(self):
        return team.git("rev-parse", "HEAD")

    def test_status_reports_overlaps_and_conflicts_with_the_baseline(self):
        self.branch("alice", "shared.py", "VALUE = 2\n")
        self.branch("bob", "shared.py", "VALUE = 3\n")
        self.branch("carol", "carol.py", "X = 1\n")
        self.advance_main("shared.py", "VALUE = 4\n")
        report = team.status()
        self.assertEqual(report["overlaps"], {"shared.py": ["origin/alice", "origin/bob"]})
        self.assertEqual(report["branches"]["origin/alice"]["conflicts_with_base"], ["shared.py"])
        self.assertEqual(report["branches"]["origin/carol"]["conflicts_with_base"], [])
        self.assertFalse(list((self.repo / ".integration" / "tmp").glob("trial-*")))   # trial worktrees removed

    def test_sync_merges_a_clean_baseline_after_green_tests(self):
        self.branch("alice", "alice.py", "A = 1\n")
        sh("git", "checkout", "-q", "alice", cwd=self.repo)
        self.advance_main("shared.py", "VALUE = 5\n")
        sh("git", "checkout", "-q", "alice", cwd=self.repo)
        self.assertEqual(team.sync(), 0)
        self.assertEqual((self.repo / "shared.py").read_text(), "VALUE = 5\n")
        self.assertEqual((self.repo / "alice.py").read_text(), "A = 1\n")             # own work kept

    def test_sync_refuses_a_conflict_and_leaves_the_branch_untouched(self):
        self.branch("alice", "shared.py", "VALUE = 2\n")
        self.advance_main("shared.py", "VALUE = 4\n")
        sh("git", "checkout", "-q", "alice", cwd=self.repo)
        before = self.head()
        self.assertEqual(team.sync(), 1)
        self.assertEqual(self.head(), before)
        self.assertEqual((self.repo / "shared.py").read_text(), "VALUE = 2\n")
        self.assertEqual(team.git("status", "--porcelain", "--untracked-files=no"), "")

    def test_sync_refuses_when_the_merged_result_breaks_the_tests(self):
        self.branch("alice", "alice.py", "A = 1\n")
        self.advance_main("tests/test_broken.py", FAILING)
        sh("git", "checkout", "-q", "alice", cwd=self.repo)
        before = self.head()
        self.assertEqual(team.sync(), 1)
        self.assertEqual(self.head(), before)

    def test_sync_never_touches_uncommitted_work(self):
        self.branch("alice", "alice.py", "A = 1\n")
        sh("git", "checkout", "-q", "alice", cwd=self.repo)
        (self.repo / "alice.py").write_text("A = 99\n")
        self.assertEqual(team.sync(), 2)
        self.assertEqual((self.repo / "alice.py").read_text(), "A = 99\n")

    def test_integrate_only_moves_the_integration_branch_when_clean_and_green(self):
        self.branch("alice", "alice.py", "A = 1\n")
        self.branch("bob", "tests/test_broken.py", FAILING)
        sh("git", "branch", "integration/next", "origin/main", cwd=self.repo)
        start = team.git("rev-parse", "integration/next")
        self.assertEqual(team.integrate("origin/bob", "integration/next"), 1)        # tests fail: unchanged
        self.assertEqual(team.git("rev-parse", "integration/next"), start)
        self.assertEqual(team.integrate("origin/alice", "integration/next"), 0)
        self.assertEqual(team.git("show", "integration/next:alice.py"), "A = 1")
        self.assertEqual(team.git("rev-parse", "origin/main"), start)                # the baseline itself untouched


if __name__ == "__main__":
    unittest.main()
