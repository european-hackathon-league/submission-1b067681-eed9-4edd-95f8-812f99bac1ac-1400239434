# How we work together (5 developers, many agents)

**`main` is the baseline**: the combined, tested project. Your branch is your experiment; work becomes shared only when
it is in `main`. Everything below is automatic or one command.

## What tells you what the others are doing

`uv run --no-sync python scripts/team.py status` (agents: `--json`). From Git alone, no file to maintain:
every branch's commits ahead/behind `main`, the files several branches change, which branches no longer merge
cleanly into `main`, and for your branch: how far behind `main` you are and who else changes your files.

On every push, CI (`.github/workflows/team.yml`) runs the tests on your branch **and on your branch merged with
`main`**, and puts the team status in the job summary. A red "merged with main" step means: sync before going on.

## Staying current (the only routine command)

`uv run --no-sync python scripts/team.py sync` merges `main` into your branch, but only if it merges cleanly AND the
tests pass on the result (tried first in a throwaway worktree under `.integration/tmp/`). Otherwise your branch is
left exactly as it was and you are told why. It never touches uncommitted work.

## How work gets into `main`

The integrator runs `scripts/team.py integrate origin/<branch>`: trial merge into `integration/next`, tests (including
`tests/test_contracts.py`), and only then the merge. A conflict or a red test changes nothing. When `integration/next`
is green it is fast-forwarded to `main` (never force-pushed). What came from where, and open questions:
`docs/INTEGRATION.md`.

Recovery: every step runs in a throwaway worktree, so a failed merge leaves no half state; `main` only moves forward
to a commit whose tests passed; old branches are never deleted or rewritten.

## Rules that prevent the problems we had

1. **Before building something, look whether it exists**: `scripts/team.py status`, `docs/INTEGRATION.md`,
   `docs/SWARM.md`. We built the analyzers three times and the robot check three times in one day.
2. **Shared interfaces are contracts with a test.** When two parts must agree (price rules, result shapes, number
   formats), add or extend a test in `tests/test_contracts.py`. Git merges text; only tests merge meaning.
3. **Small, short-lived branches** off `main`, merged back within a day. A branch that has not seen `main` for hours
   is building on yesterday's project.
4. **One agent, one task, one worktree**: `git worktree add ../<task> -b <you>/<task> origin/main`. Two agents never
   edit in the same worktree.
5. Never commit `.integration/`, `.swarm/`, `.env` (all git-ignored); never force-push shared branches.
