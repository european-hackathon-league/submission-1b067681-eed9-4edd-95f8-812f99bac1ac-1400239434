# Integration record

The baseline (`main`) is the one project everyone builds on. This file says what came from which branch, what was
superseded by what, and which questions are still open. Update it when you integrate something (`scripts/team.py`).

## Baseline 2026-10-10: `integration/next` (main + tender_portal + site-recon + ports)

| Branch (author) | What it brought | Status in the baseline |
| --- | --- | --- |
| `main` (ksiox1337, base) | `swarm/bidmath.py` (bid rules as tested functions), `swarm/formpilot.py` + formpilot submitter, `swarm/decide.py` (Clef), `SITE-NOTES.md` | Merged. The formpilot **submitter** was replaced (conflict, see below); `bidmath`, `formpilot`, `decide` are kept and tested but **no runtime agent calls them**. |
| `site-recon` (kurk-s) | `sites.py` site plumbing (login, cookie wall, ERP error page, canvas reading, unlock slot), `tender_model.py`, site notes, nominal stroke for "nutzbarer Weg" in `bidmath` | Merged fully, no conflicts. `sites.py` and `tender_model.py` are **not called by the runtime**. |
| `tender_portal` (CRUBSTERINO) | The runtime: dataflow COLLECT with analysts, collection coordinator (gaps -> follow-up jobs), popup/access guard, `read_page`, tabs, flash mode, receipt-confirmed signing with built-in TAN lookup, `docs/REVIEW.md` (20 known problems), signing/navigation evaluation scripts | Merged fully. **The runtime of the baseline.** |
| `test_brief_reader` (Maksym Shvydenko) | Brief reader with value validation and login masking, rules engine on `bidmath` with follow-up rounds, answer validator, robot-check module, `read_page_text`, TAN fetcher | Not merged (a parallel rewrite of the same 18 files). **Ported:** login masking and "value must be in the brief" checks into `agents/read_brief.py`. **Superseded:** engine -> `agents/compute_bid.py` (rules agree, see contracts); validator -> `swarm/validation.py`; robot/gate -> `swarm/guard.py`; `read_page_text` -> `swarm/page_text.py`; follow-ups -> `agents/coordinate.py`; TAN fetcher -> submitter's TAN lookup. |
| `tanreader-debug` (ksiox1337 + a Claude session, local commit `1fb11af`) | TanReader agent, formpilot fixes (loop guard, no invented TAN, binding confirmation), browser-use in one process per agent (bubus global lock), frozen-tab replacement, page capture, RUNSTATS (tokens/cost per browse) | Not merged (a parallel rewrite of 15 of the same files). **Superseded:** analyzers/calculator, access check, page capture, Qwen settings (all exist in tender_portal's runtime). **Open:** process isolation and RUNSTATS (below). |

Conflict resolved by intent: `agents/submit_bid.py` (main's formpilot submitter vs tender_portal's browser-use
submitter) -> tender_portal's: newer, receipt-confirmed, with a signing regression script. main's submitter tests were
rewritten for it (`tests/test_submit_bid.py`, same intents).

## Contracts (checked by tests, not by Git)

- **Price rules**: `tests/test_contracts.py` feeds `agents/compute_bid.compute` and `swarm/bidmath` the same
  hand-worked case (FX, metal index, clauses, cartons, setup pool, bonus bands, commitment, shared stock, conditional
  discount). Both agree on every field. Change either and this test tells you whether they still agree.
- **Sandbox network**: model HTTP clients must keep httpx's default transport (HTTPS_PROXY); a custom transport
  bypassed the sandbox proxy and every call failed (found on `tanreader-debug`).

## Open questions (decide as a team, then change one place)

| Question | Today | Where |
| --- | --- | --- |
| Number format in the bid form above 999 EUR | **Settled**: the form accepted `2.008,33` / `139.604,98` (run_7732ba545b50b3a5, quality 59.2). | `swarm/normalize.de_number` |
| Condition without a price unit | runtime prices nothing (tender_portal: "does not get guessed"); bidmath assumes 1 | `agents/compute_bid.scale_price` |
| "Ordinary setup fee" source | runtime: from the Q&A answer; tanreader-debug's calculator: from the ERP condition | `agents/compute_bid.position_keys` |
| A browser that stops answering | **Fixed after the first sandbox run of this baseline** (run_2fc8121504807328: browser 0 never reconnected, 0 tender pages, 5 failed signing attempts): `browse()` checks the browser and moves the work to a new tab of a responsive one (`responsive_tab`). | `swarm/browser_agent.py` |
| A hung tab blocks connecting to its browser | **Fixed** (run_f93abf054739c02c: the browser still answered, but browser-use's connect attaches to every tab and timed out on the hung one; the submitter failed 5x): `responsive_tab` also closes tabs that cannot evaluate `1` within 8 s (`close_hung_tabs`, tested against real Chromium). | `swarm/browser_agent.py` |
| Sandbox CPU load (4 vCPU) | ERP tabs now 6 (see below); mail topic tabs 2, guard sweep 4 s. Evidence: two sandbox runs stalled every browser at once; the same main locally ran 85 s without a single stall. Tunable per run: `SWARM_ERP_TABS`, `SWARM_MAIL_TABS`, `SWARM_GUARD_INTERVAL_S`. | `agents/scrape_erp.py`, `agents/scrape_mail.py`, `swarm/guard.py` |
| **ERP prices are the bottleneck (largest score lever)** | Varies run to run: 4 of 5 materials priced (run_7732ba545b50b3a5, quality 59.2), 0 of 5 (run_aa35ba17eddd5994). The ERP agents spend 6-12 steps per material on MM03 records (404s on guessed tab URLs, error pages) before the price jobs (VK13, header + separate scales register) start; collection ends first. **Addressed** by dependency-driven ERP scheduling (someimprovementsidk, CRUBSTERINO): prices/stock of a matched material run before further records; records of covered positions last. | `agents/scrape_erp.py`, `agents/analyze_materials.py` |
| One frozen tab stalls every browser | browser-use routes all browser events of a process through one global lock (bubus); tanreader-debug isolated each browse in its own process, tender_portal ends a stalled connection after two timeouts and reattaches. Not reconciled: the isolation does not fit tender_portal's in-process tabs. | `swarm/browser_agent.py` |
| Tokens/cost per agent in every run log (RUNSTATS) | only on tanreader-debug | `swarm/orchestrator.py` |
| Unused modules (`bidmath`, `formpilot`, `decide`, `sites.py`, `tender_model.py`) | kept and tested; runtime does not call them | delete or adopt |
| Recovery closing other agents' tabs | **Fixed**: `close_hung_tabs` skips tabs another browse is using (`_ACTIVE`); tested on real Chromium. | `swarm/browser_agent.py` |
| Fixed 600 s submit reserve | Now estimated live from positions and measured step pace, [390, 600] s, calibrated on sandbox signings (run_7a11d3a90d55e1ce: accepted 15:40, 260 s to spare). | `swarm/context.py` |
| Sandbox CPU vs ERP tabs | 6 tabs beat 4 in an A/B (17 vs 12 ERP jobs, 4 vs 1 positions priced, fewer failed browses); one run per arm. | `agents/scrape_erp.py` |
| Condition fragments of different records merged | **Fixed**: `reconcile_conditions` (record ids, linked pages, conflicts kept apart). | `agents/analyze_materials.py` |
| Tokens/time per agent in every run log (RUNSTATS) | Partly covered by `swarm/timeline.py` (time per agent and category, milestones, counters). | `swarm/timeline.py` |
| team.py checks red without pytest | **Fixed**: falls back to `scripts/run_tests.py` (unittest + plain test functions). | `scripts/team.py` |
