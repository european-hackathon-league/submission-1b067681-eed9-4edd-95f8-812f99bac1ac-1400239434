"""Team coordination from Git alone: what every branch is doing, where branches collide, and safe syncing.

    uv run --no-sync python scripts/team.py status            # every branch vs main: ahead/behind, overlaps, conflicts
    uv run --no-sync python scripts/team.py status --json     # the same, machine-readable (for agents)
    uv run --no-sync python scripts/team.py sync              # merge origin/main into the current branch, safely
    uv run --no-sync python scripts/team.py integrate <branch> [--into integration/next]

Nothing here needs a server or a file someone keeps up to date: every fact comes from `git`. Trial merges and test
runs happen in throwaway worktrees under .integration/tmp/ (git-ignored), so a failed merge never touches a working
branch: `sync` and `integrate` change a branch only after the merge applied cleanly AND the tests passed there.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = "origin/main"
SKIP = ("origin/HEAD", "origin/entire/")       # Entire's shadow branches are not developer work
TMP = ".integration/tmp"
TEST_CMD = [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"]


def git(*args, cwd=None, check=True) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def root() -> Path:
    return Path(git("rev-parse", "--show-toplevel"))


def branches(base: str = BASE) -> list[str]:
    refs = git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin").splitlines()
    return [r for r in refs if r != base and not r.startswith(SKIP) and r != "origin"]


def changed_files(branch: str, base: str = BASE) -> list[str]:
    return [f for f in git("diff", "--name-only", f"{base}...{branch}").splitlines() if f]


class Trial:
    """A throwaway worktree for a trial merge; always removed again, whatever happens inside."""

    def __init__(self, start: str):
        self.start, self.path = start, None

    def __enter__(self) -> Path:
        base = root() / TMP
        base.mkdir(parents=True, exist_ok=True)
        self.path = Path(tempfile.mkdtemp(prefix="trial-", dir=base))
        git("worktree", "add", "--detach", str(self.path), self.start)
        return self.path

    def __exit__(self, *exc):
        git("worktree", "remove", "--force", str(self.path), check=False)
        shutil.rmtree(self.path, ignore_errors=True)
        git("worktree", "prune", check=False)


def trial_merge(target: str, incoming: str, run_tests: bool = False) -> dict:
    """Merge `incoming` into `target` in a throwaway worktree: conflicts, and (optionally) the test result."""
    with Trial(target) as path:
        merged = subprocess.run(["git", "merge", "--no-commit", "--no-ff", incoming], cwd=path,
                                capture_output=True, text=True)
        conflicts = [f for f in git("diff", "--name-only", "--diff-filter=U", cwd=path).splitlines() if f]
        result = {"clean": merged.returncode == 0 and not conflicts, "conflicts": conflicts, "tests": None}
        if result["clean"] and run_tests:
            tests = subprocess.run(TEST_CMD, cwd=path, capture_output=True, text=True)
            lines = (tests.stdout or tests.stderr).strip().splitlines()
            result["tests"] = {"passed": tests.returncode == 0, "summary": lines[-1] if lines else ""}
        git("merge", "--abort", cwd=path, check=False)
        return result


def status(base: str = BASE, conflicts: bool = True) -> dict:
    """Every branch vs the baseline, the files several branches change, and (for the current branch) what is new."""
    report = {"base": base, "branches": {}, "overlaps": {}}
    touched: dict[str, list[str]] = {}
    for b in branches(base):
        files = changed_files(b, base)
        info = {
            "ahead": int(git("rev-list", "--count", f"{base}..{b}")),
            "behind": int(git("rev-list", "--count", f"{b}..{base}")),
            "last": git("log", "-1", "--format=%ad %an: %s", "--date=format:%m-%d %H:%M", b),
            "files": files,
        }
        info["in_current"] = subprocess.run(["git", "merge-base", "--is-ancestor", b, "HEAD"]).returncode == 0
        if conflicts and info["ahead"]:
            info["conflicts_with_base"] = trial_merge(base, b)["conflicts"]
        report["branches"][b] = info
        for f in files:
            touched.setdefault(f, []).append(b)
    report["overlaps"] = {f: bs for f, bs in sorted(touched.items()) if len(bs) > 1}
    current = git("rev-parse", "--abbrev-ref", "HEAD")
    if current != "HEAD":
        mine = set(git("diff", "--name-only", f"{base}...HEAD").splitlines())
        new_on_base = git("diff", "--name-only", f"HEAD...{base}").splitlines()
        report["current"] = {
            "branch": current,
            "behind_base": int(git("rev-list", "--count", f"HEAD..{base}")),
            "base_changed_files_you_also_changed": sorted(mine & set(new_on_base)),
            "others_changing_your_files": {f: others for f in sorted(mine)
                                           if (others := [b for b in touched.get(f, [])
                                                          if not b.endswith("/" + current)
                                                          and not report["branches"][b]["in_current"]])},
        }
    return report


def print_status(report: dict) -> None:
    print(f"# Team status (baseline {report['base']})\n")
    for b, info in report["branches"].items():
        conflict = info.get("conflicts_with_base")
        flag = f" CONFLICTS with baseline: {', '.join(conflict)}" if conflict else ""
        merged = " [already in your branch]" if info.get("in_current") else ""
        print(f"- {b}: {info['ahead']} ahead / {info['behind']} behind, {len(info['files'])} files. "
              f"Last: {info['last']}.{flag}{merged}")
    if report["overlaps"]:
        print("\n## Files changed on more than one branch (coordinate before editing)")
        for f, bs in report["overlaps"].items():
            print(f"- {f}: {', '.join(bs)}")
    cur = report.get("current")
    if cur:
        print(f"\n## You are on {cur['branch']}: {cur['behind_base']} commits behind {report['base']}")
        if cur["base_changed_files_you_also_changed"]:
            print("Baseline changed files you changed too (merge it before going on): "
                  + ", ".join(cur["base_changed_files_you_also_changed"]))
        for f, bs in cur["others_changing_your_files"].items():
            print(f"- your {f} is also changed on {', '.join(bs)}")
        if cur["behind_base"]:
            print("Bring the baseline in safely: uv run --no-sync python scripts/team.py sync")


def sync(base: str = BASE, run_tests: bool = True) -> int:
    """Merge the baseline into the current branch, only if it merges cleanly and the tests pass on the result."""
    if git("status", "--porcelain", "--untracked-files=no"):
        print("Uncommitted changes: commit them first (sync never touches a dirty worktree).")
        return 2
    git("fetch", "--quiet", "origin", check=False)
    if not int(git("rev-list", "--count", f"HEAD..{base}")):
        print(f"Already up to date with {base}.")
        return 0
    trial = trial_merge("HEAD", base, run_tests=run_tests)
    if not trial["clean"]:
        print(f"Not merged: {base} conflicts with your branch in {', '.join(trial['conflicts'])}.\n"
              "Your branch is unchanged. Resolve with: git merge origin/main (or ask the integrator).")
        return 1
    if run_tests and not trial["tests"]["passed"]:
        print(f"Not merged: tests fail on the merged result ({trial['tests']['summary']}). Your branch is unchanged.")
        return 1
    git("merge", "--no-edit", base)
    print(f"Merged {base} into {git('rev-parse', '--abbrev-ref', 'HEAD')}"
          + (f"; tests on the merge: {trial['tests']['summary']}" if run_tests else "") + ".")
    return 0


def integrate(branch: str, into: str, run_tests: bool = True) -> int:
    """Bring one developer branch into the integration branch, only when it merges cleanly and the tests pass."""
    trial = trial_merge(into, branch, run_tests=run_tests)
    if not trial["clean"]:
        print(f"Not integrated: {branch} conflicts with {into} in {', '.join(trial['conflicts'])}. "
              "These need a deliberate resolution (see docs/INTEGRATION.md); nothing was changed.")
        return 1
    if run_tests and not trial["tests"]["passed"]:
        print(f"Not integrated: tests fail after merging {branch} ({trial['tests']['summary']}). Nothing was changed.")
        return 1
    worktrees = git("worktree", "list", "--porcelain")
    checked_out = f"branch refs/heads/{into}" in worktrees
    if checked_out:
        print(f"{branch} merges cleanly into {into} and tests pass; {into} is checked out elsewhere, so merge it "
              f"there: git merge --no-ff {branch}")
        return 0
    with Trial(into) as path:
        git("merge", "--no-ff", "--no-edit", branch, cwd=path)
        git("branch", "-f", into, git("rev-parse", "HEAD", cwd=path))
    print(f"Integrated {branch} into {into}" + (f" ({trial['tests']['summary']})" if run_tests else "") + ".")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    st = sub.add_parser("status")
    st.add_argument("--json", action="store_true")
    st.add_argument("--no-fetch", action="store_true")
    st.add_argument("--no-conflicts", action="store_true", help="skip the trial merges (faster)")
    sy = sub.add_parser("sync")
    sy.add_argument("--no-tests", action="store_true")
    sy.add_argument("--base", default=BASE, help="what to merge in (default origin/main)")
    ig = sub.add_parser("integrate")
    ig.add_argument("branch")
    ig.add_argument("--into", default="integration/next")
    ig.add_argument("--no-tests", action="store_true")
    args = parser.parse_args(argv)
    os.chdir(root())
    if args.cmd in (None, "status"):
        if not getattr(args, "no_fetch", False):
            git("fetch", "--quiet", "--prune", "origin", check=False)
        report = status(conflicts=not getattr(args, "no_conflicts", False))
        print(json.dumps(report, indent=1)) if getattr(args, "json", False) else print_status(report)
        return 0
    if args.cmd == "sync":
        return sync(base=args.base, run_tests=not args.no_tests)
    return integrate(args.branch, args.into, run_tests=not args.no_tests)


if __name__ == "__main__":
    sys.exit(main())
