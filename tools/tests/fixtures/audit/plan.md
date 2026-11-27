# 0.9.1: Teach it to count

- **Kind:** patch, one batch.
- **Title:** `0.9.1: Teach it to count`
- **Sitting:** no sitting: no behaviour change

<!-- A made-up release that is ready to merge, for
     tools/tests/test_release_audit.py: every stage to 7 exited or skipped,
     no S1 or S2 left open, no flag gap. Each test copies this file, changes
     one thing in it and sees what the audit says of it. -->

## 0. Scope

| Task (roadmap wording, id) | Status | File | Test | Caller | Batch |
|---|---|---|---|---|---|
| Count to ten (T1) | done | `count.py` | `test_count.py` | the engine | A |

## 1. Decisions

None bear on this release.

## 2. Stages

| Stage | Entered | Exited | Exit criterion | Evidence (path under releases/v0.9.1/) |
|---|---|---|---|---|
| 0. Scope and research | 2026-11-20 | 2026-11-21 | A line for every check | `brief.md` |
| 1. Contract and freeze | 2026-11-21 | 2026-11-22 | Frozen, with a date | `contract.md` |
| 2. Build | 2026-11-22 | 2026-11-24 | Green on the laptop | |
| 3. Review and fix | 2026-11-24 | 2026-11-26 | No open S1 | |
| 4. Body evidence | 2026-11-26 | 2026-11-28 | A passing run | `body/2026-11-30/` |
| 5. Tune | | skipped: no default changes | Only when a default changes | |
| 6. API and UX gate | 2026-11-28 | 2026-11-29 | The console sheet complete | |
| 7. Candidate and sitting | 2026-11-29 | 2026-11-30 | The evidence check | |
| 8. Ship | | | The audit passes | |
| 9. Close | | | The next brief exists | |

## 3. Budgets and measurements

**Flags not run on the body in this release:**

| Flag not run | Reason | Answer | Date |
|---|---|---|---|

## 4. Ledger

| ID | Found (stage, date, by) | Severity | Reproduced how | Regression test, or the reason there is none | State | Fixed in |
|---|---|---|---|---|---|---|
| L1 | Stage 3, 2026-11-25, review | S2 | Ran it twice | `test_count.py::test_ten` | fixed | 0.9.1 |
| L2 | Stage 3, 2026-11-25, review | S3 | By hand | none: a wording fix | open | |
| L3 | Stage 4, 2026-11-27, the body | S4 | On the Pi | none | closed: a duplicate of L2 | |

## 5. Register rows touched

None.
