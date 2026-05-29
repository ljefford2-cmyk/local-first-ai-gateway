# Plan: KI-2 — Close the modify-branch first-writer TOCTOU in `override_job`

## Objective
Close the time-of-check-to-time-of-use race in the `modify` branch of
`orchestrator/job_manager.py:override_job()` so that concurrent
`override_job(..., "modify", ...)` calls on one `job_id` serialize
first-writer-wins, with losers returning `no_op / already_overridden` and
emitting no durable audit events. Durable emit order is preserved; no schema,
confidence, or audit-event-shape change is made.

## Scope
- `orchestrator/job_manager.py`, `override_type == "modify"` branch only:
  set the first-writer guard `job.override_type = "modify"` (with a synchronous
  `_persist_job(job)`) immediately after the modify-branch validation and
  before the first durable `human.override` emit. Mechanism A (lock-free early
  guard-set), mirroring the existing `proposal_ready` pre-`await` set and the
  review handlers.
- The existing later `job.override_type = "modify"` assignment is left in place
  as a no-op rewrite.
- Tests in `tests/test_phase5d.py`: a red-green concurrency reproduction
  (modify-vs-modify), an audit no-laundering / event-count assertion, a
  guard-before-emit observation, and the existing event-order regressions kept
  green.
- `STATUS.md`: resolve KI-2 for the modify branch; open KI-3 for the sibling
  `cancel`/`redirect`/`escalate` late-guard races on non-`proposal_ready`
  states.

## Out of Scope
- `confidence` / Rule A / `confidence_source`.
- Schema or audit-event payload-shape changes.
- Reordering durable audit emits.
- Broad refactor of `override_job()`.
- Per-job lock / broad serialization infrastructure.
- Fixing the `cancel`/`redirect`/`escalate` late-guard races (tracked as KI-3).
- Review semantics; WAL demotion behavior.
- Cleanup-only deletions on audit-sensitive paths.

## Files Likely Affected
- `orchestrator/job_manager.py` — one additive insertion in the `modify`
  branch (early guard-set + synchronous persist).
- `tests/test_phase5d.py` — new concurrency / no-laundering / guard-before-emit
  tests; existing modify event-order tests retained.
- `STATUS.md` — KI-2 resolution note; new KI-3 carry-forward.
- `docs/plans/ki-2-modify-toctou.md` [NEW] — this artifact.

## Risks
- **Test does not actually exercise the race (medium).** `MockAuditClient.emit_durable`
  does not suspend, so a naive `asyncio.gather` of two modify calls would not
  interleave and would pass even against unpatched code. Mitigation: the gated
  audit spy introduces a real suspension point at the first `human.override`
  emit; the concurrency test is red-green verified (must FAIL on unpatched HEAD,
  PASS after the fix), and both runs are captured in the report.
- **Reopening the window via a new yield (low).** Mitigation: the inserted
  `_persist_job` is synchronous (no `await`); the only awaits in the branch
  remain the three durable emits, all after the guard-set. Verified there is no
  `await` between the entry guard and the insertion point.
- **Audit ordering perturbed / event laundered (low, high-consequence).**
  Mitigation: only an in-memory assignment (+ synchronous persist) moves; no
  `emit_durable` call changes position. Regression tests assert the exact
  3-event (`response_received`) and 2-event (`delivered`) orders. This is the
  §0 forbidden move and is explicitly avoided.
- **Scope creep into sibling branches (low).** Mitigation: change is confined to
  the `modify` elif; KI-3 records the sibling races as future work so green
  modify tests are not misread as whole-override serialization.

## Acceptance Criteria
1. [file-state-verifiable] In `job_manager.py`, the `modify` branch sets
   `job.override_type = "modify"` followed by `self._persist_job(job)` after the
   `already_delivered` line and before the `human.override` emit. Confirm by
   reading the branch.
2. [test-verifiable] Two concurrent `override_job(..., "modify", ...)` on one
   `job_id` yield exactly one `status == "modified"` and one
   `status == "no_op"` / `reason == "already_overridden"`; only one modified
   result artifact is written. (`tests/test_phase5d.py` concurrency test.)
3. [test-verifiable] The loser emits no durable events: total chain equals the
   winner only — 3 events (`human.override`, `human.reviewed`, `job.delivered`)
   for `response_received`; 2 (`human.override`, `human.reviewed`) for
   `delivered`. (No-laundering test.)
4. [test-verifiable] `job.override_type == "modify"` at the moment
   `human.override` is emitted. (Guard-before-emit observation test.)
5. [test-verifiable] Non-concurrent modify behavior and durable emit order are
   unchanged: `test_modify_response_received_job` and `test_modify_delivered_job`
   pass without modification to their assertions.
6. [behavior-verifiable] The concurrency test FAILS on unpatched HEAD and PASSES
   after the fix. Observation: run
   `python -m pytest tests/test_phase5d.py -k concurrent -v` before applying the
   `job_manager.py` change (expect fail) and after (expect pass); both outputs
   captured in the report.
7. [file-state-verifiable] `STATUS.md` KI-2 is tagged resolved for the modify
   branch only, and a KI-3 entry exists for the sibling late-guard races.
8. [file-state-verifiable] The diff touches only the four files above; no
   `confidence`, schema, audit-event-shape, or `cancel`/`redirect`/`escalate`
   implementation change appears in `git diff`.

## Human Approval Required
YES — §9.2 category 6 (audit / emit-durable semantics) and category 7
(first-writer / review-gate authority). Plan approved by Lawrence prior to
implementation; commit gate remains (stop before commit).

## Role Assignments
Planner: Claude Code (KI-2 Part 1 discovery + Part 2 plan validation).
Builder: Claude Code (this session).
Verifier: Claude Code (pytest ladder, red-green capture).
Critic: Lawrence (human approval gates + adversarial review of the diff).
Reporter: Claude Code (pre-commit report).

## Reserved-Terms Check
Reviewed for §6.1 collisions: YES. No build-workflow redefinition of `pipeline`,
`dispatch`, `capability`, `promotion`/`demotion`, or `governing`/`auxiliary`;
all such terms appear only in their runtime sense.
