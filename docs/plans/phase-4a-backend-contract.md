# Plan: Phase 4A Mobile Agent Command Harness — Backend Contract

## Objective

Establish the minimum gateway-side backend contract required to prove the governed Agent Proposal / Agent Inbox approval loop:

mobile-origin job → proposal-ready state → inbox enumeration → human decision → committed, deferred, rejected, or declined outcome with audit-visible lineage.

Phase 4A creates a stable HTTP, state, and audit contract against which a future iPhone/Watch client can be built. It does not implement native mobile client code, push delivery, APNs, or a formal orchestration-side Spec 9.

## Scope

- Extend `JobSubmitRequest` to preserve mobile-origin lineage:
  - `client_source`: `phone_app | watch_app`
  - `client_source_event_id`: UUIDv7
  - `client_timestamp`: ISO 8601 timestamp
  - Preserve the existing `device: phone | watch` field as device class, distinct from audit source identity.
- Add a non-terminal `proposal_ready` job status.
  - Jobs held for human review by the existing review-gate logic must surface as `proposal_ready` instead of silently proceeding.
  - A `proposal_ready` job remains pending until a review decision, override, or terminal failure.
- Add a structured `Proposal` model surfaced on `JobStatusResponse` for jobs in `proposal_ready` status, otherwise omitted or `null`.
  - Required fields:
    - `proposal_id`
    - `job_id`
    - `result_id`
    - `response_hash`
    - `proposed_by`
    - `governing_capability_id`
    - `confidence`
    - `auto_accept_at`
  - `auto_accept_at` is an absolute ISO 8601 timestamp when applicable, otherwise `null`.
  - Do not include `available_actions` in Phase 4A.
  - Do not include `confidence_band` in Phase 4A.
- Add a pending-job enumeration endpoint for Agent Inbox polling.
  - Implement `GET /jobs?status={status}&since={cursor}&limit={n}`.
  - The endpoint must allow a phone client to discover outstanding review items after foreground, reconnect, restart, or reinstall.
- Add a public review endpoint:
  - `POST /jobs/{job_id}/review`
  - Supported decisions:
    - `approve`
    - `edit`
    - `reject`
    - `defer`
    - `decline_to_act`
- Implement locked Phase 4A decision semantics:
  - `approve`: commit the current result and transition to `delivered`.
  - `edit`: commit the edited result, preserve modified-result lineage, and transition to `delivered`.
  - `reject`: no commit; transition to `failed`; count as negative review evidence.
  - `defer`: no commit; keep the job in `proposal_ready`; neutral for WAL counters.
  - `decline_to_act`: no commit; transition to a new terminal-neutral status `closed_no_action`; neutral for WAL counters.
- Add stale-decision protection on `POST /jobs/{job_id}/review`.
  - Review requests must include `result_id` and `response_hash`.
  - If either value does not match the current authoritative result, return HTTP `409 Conflict`.
  - The 409 response body must include:
    - `error`
    - `current_result_id`
    - `current_response_hash`
    - `current_status`
    - human-readable `message`
- Add review-decision idempotency.
  - Review requests must include a client-supplied `decision_idempotency_key`.
  - Replayed decisions with the same key must return the original outcome without repeating state transitions or emitting duplicate durable decision events.
  - Extend the existing `IdempotencyStore` with a separated review-decision namespace prefix (e.g., `review_idem:{decision_idempotency_key}`). Do not create a new store.
- Use `extra="forbid"` for all new or modified request models touched by Phase 4A:
  - extended `JobSubmitRequest`
  - new `ReviewRequest`
- Add tests proving the contract.
- Update `STATUS.md` and `docs/SPEC-MAP.md` to reflect the new Phase 4A contract and evidence.

## Out of Scope

- Native iOS, Swift, Watch, WatchConnectivity, SwiftData, Core Data, or Apple Watch implementation.
- Push notifications or APNs.
- Spec 9 creation or orchestration-side formalization of proposal/approval semantics.
- General inbound device-event ingestion such as `POST /events/inbound`.
- G8 phone/watch-originated `system.connectivity` event ingestion.
- G9 cleanup of inline `OverrideRequest` placement.
- G11 idempotency auto-purge scheduling.
- `available_actions` on the proposal object.
- `confidence_band` on the proposal object.
- Per-job auto-accept configuration.
- Broader WAL promotion/demotion redesign.
- Mobile UI/UX design beyond the backend response shapes required for a future Agent Inbox.

## Files Likely Affected

- `orchestrator/models.py`
  - Add `Proposal` model.
  - Add `JobStatus.proposal_ready`.
  - Add `JobStatus.closed_no_action`.
  - Extend `JobSubmitRequest`.
  - Extend `JobStatusResponse` with `proposal`.
- `orchestrator/main.py`
  - Add public review endpoint.
  - Add pending-job enumeration endpoint.
  - Update `POST /jobs` handling for mobile-origin submit fields.
- `orchestrator/job_manager.py`
  - Preserve mobile-origin lineage through submit.
  - Wire held/pre-delivery review behavior into `proposal_ready`.
  - Build proposal objects.
  - Implement review decision handling.
  - Implement `closed_no_action`.
  - Implement stale-decision checks.
  - Implement review-decision idempotency behavior.
- `orchestrator/events.py`
  - Allow `job.submitted` event construction to use client-supplied source, source event ID, and timestamp when present.
  - Add or extend `human.reviewed` decision values for `deferred` and `declined`.
- `orchestrator/idempotency_store.py`
  - Extend with separated namespace prefix for review decisions.
- `orchestrator/persistence.py`
  - Add query support if status-based enumeration cannot be served from existing job state access.
- `[NEW] tests/test_phase4a_backend_contract.py`
  - Tests for mobile-origin lineage, proposal state, proposal shape, enumeration, review decisions, stale conflict, idempotency, and extra-field rejection.
- `STATUS.md`
  - Add or update claim-status-evidence rows for Phase 4A backend contract behavior.
- `docs/SPEC-MAP.md`
  - Map Phase 4A backend contract additions to relevant specs and implementation files.

## Risks

- **Risk: `extra="forbid"` may break clients that currently send unknown fields.**
  - Severity: Medium.
  - Mitigation: Phase 4A has no production mobile client yet. Add backward-compatibility tests for valid existing `POST /jobs` requests and add explicit 422 tests for unknown fields.
- **Risk: new `closed_no_action` status may be missed by downstream consumers.**
  - Severity: Medium.
  - Mitigation: Add tests that all status serialization, review handling, persistence, and WAL-neutral paths recognize `closed_no_action`.
- **Risk: `proposal_ready` jobs may accumulate indefinitely after repeated `defer` decisions.**
  - Severity: Low.
  - Mitigation: This is intentional for Phase 4A. No TTL or auto-expiry is introduced. Track as future inbox hygiene work after the harness proves the loop.
- **Risk: stale-decision checks could reject valid decisions if result identity is not consistently maintained.**
  - Severity: Medium.
  - Mitigation: Require tests for matching and mismatching `result_id` and `response_hash`. Restrict result identity changes to clear result-producing paths such as initial response and edit/modify.
- **Risk: the contract could expand during implementation.**
  - Severity: High.
  - Mitigation: Acceptance criteria explicitly forbid new proposal fields, new job statuses, new decisions, and new endpoints beyond those named in this plan unless the plan is amended.
- **Risk: the plan could drift into mobile-client implementation.**
  - Severity: Medium.
  - Mitigation: Acceptance criteria explicitly verify that no iOS, Swift, WatchConnectivity, APNs, or mobile-client files are added.

## Acceptance Criteria

1. [file-state-verifiable] `orchestrator/models.py` defines `JobStatus.proposal_ready` and `JobStatus.closed_no_action`, and no other Phase 4A job statuses are added.
2. [file-state-verifiable] `orchestrator/models.py` defines a `Proposal` model with exactly these fields: `proposal_id`, `job_id`, `result_id`, `response_hash`, `proposed_by`, `governing_capability_id`, `confidence`, and `auto_accept_at`.
3. [file-state-verifiable] The `Proposal` model does not include `available_actions` or `confidence_band`.
4. [file-state-verifiable] `JobSubmitRequest` includes `client_source`, `client_source_event_id`, and `client_timestamp`, and uses `extra="forbid"`.
5. [file-state-verifiable] `ReviewRequest` exists for `POST /jobs/{job_id}/review`, requires `decision`, `result_id`, `response_hash`, and `decision_idempotency_key`, and uses `extra="forbid"`.
6. [test-verifiable] A test asserts mobile-origin lineage preservation: a job submitted with `client_source`, `client_source_event_id`, and `client_timestamp` produces an emitted `job.submitted` event that preserves those values.
7. [test-verifiable] A backward-compatibility test submits a job without `client_source`, `client_source_event_id`, or `client_timestamp`, and asserts the emitted `job.submitted` event has `source="orchestrator"`, a server-generated UUIDv7 `source_event_id`, and a server-generated ISO 8601 `timestamp`.
8. [test-verifiable] A test drives a job into the human-review path and asserts the job reaches `proposal_ready` instead of silently proceeding.
9. [test-verifiable] A test asserts `GET /jobs/{job_id}` returns a populated `proposal` object for a `proposal_ready` job.
10. [test-verifiable] A test asserts `proposal.auto_accept_at` is `null` when no auto-accept applies and an ISO 8601 absolute timestamp when auto-accept applies.
11. [behavior-verifiable] Pending proposal jobs can be enumerated by calling `GET /jobs?status=proposal_ready&limit=50`; the response returns a list containing only jobs whose status is `proposal_ready`.
12. [behavior-verifiable] Pagination behavior is observable by calling `GET /jobs?status=proposal_ready&since={cursor}&limit={n}` and confirming the next response advances without duplicating already returned jobs.
13. [test-verifiable] Review decision `approve` transitions a `proposal_ready` job to `delivered` and emits `human.reviewed` with `decision="accepted"`.
14. [test-verifiable] Review decision `edit` transitions a `proposal_ready` job to `delivered`, stores the modified result, and preserves modified-result lineage.
15. [test-verifiable] Review decision `reject` transitions a `proposal_ready` job to `failed` and emits `human.reviewed` with `decision="rejected"`.
16. [test-verifiable] Review decision `defer` leaves the job in `proposal_ready`, emits `human.reviewed` with `decision="deferred"`, and does not increment promotion or demotion counters.
17. [test-verifiable] Review decision `decline_to_act` transitions the job to `closed_no_action`, emits `human.reviewed` with `decision="declined"`, and does not increment promotion or demotion counters.
18. [behavior-verifiable] `POST /jobs/{job_id}/review` with a stale `result_id` returns HTTP `409 Conflict`; the response body includes `error`, `current_result_id`, `current_response_hash`, `current_status`, and `message`.
19. [behavior-verifiable] `POST /jobs/{job_id}/review` with a stale `response_hash` returns HTTP `409 Conflict`; the response body includes `error`, `current_result_id`, `current_response_hash`, `current_status`, and `message`.
20. [test-verifiable] Replaying `POST /jobs/{job_id}/review` with the same `decision_idempotency_key` returns the original outcome without duplicate state transitions.
21. [test-verifiable] Replaying `POST /jobs/{job_id}/review` with the same `decision_idempotency_key` does not emit a duplicate durable `human.reviewed` event.
22. [test-verifiable] Review-decision idempotency records persist across restart or recreated idempotency-store state, using the existing SQLite-backed persistence path.
23. [behavior-verifiable] `POST /jobs` with an unknown request field returns HTTP `422`.
24. [behavior-verifiable] `POST /jobs/{job_id}/review` with an unknown request field returns HTTP `422`.
25. [file-state-verifiable] No iOS, Swift, WatchConnectivity, APNs, or native mobile-client files are added by this plan.
26. [file-state-verifiable] No `audit-log-writer` source files are modified by this plan.
27. [file-state-verifiable] `STATUS.md` is updated with Phase 4A claim-status-evidence rows or notes reflecting the implemented backend contract.
28. [file-state-verifiable] `docs/SPEC-MAP.md` is updated to map the Phase 4A backend contract endpoints, models, and tests to their implementation files.
29. [test-verifiable] Existing pre-Phase-4A tests for `POST /jobs`, `GET /jobs/{job_id}`, override paths, idempotency, audit writing, hub state, and persistence continue to pass unless a plan amendment explicitly identifies and approves a test update.
30. [file-state-verifiable] No new `human.reviewed` decision values are added beyond `"deferred"` and `"declined"` for Phase 4A.
31. [file-state-verifiable] No new HTTP routes are registered beyond `POST /jobs/{job_id}/review` and `GET /jobs` for Phase 4A.

## Human Approval Required

YES.

This is an Elevated build under SPEC-8 because it changes the gateway API contract, job schema, review semantics, audit lineage handling, and capability-adjacent human decision behavior. Specifically, this work qualifies for Elevated under §9.2 categories covering schema changes, audit-event extensions, and capability policy, and under §9.0 for touching more than three files.

Human approval of this plan is required before Builder work begins. Approval means the operator authorizes implementation of the bounded contract described in this plan. Approval does not create a new orchestration-side Spec 9 and does not authorize iOS/Watch implementation.

## Role Assignments

Planner: Lawrence Jeffords + Claude project planning session, with ChatGPT review assistance.

Builder: Claude Code against `C:\Users\ljeff\drnt-project`.

Verifier: Claude Code plus pytest suite, using SPEC-8 verifier rules and the acceptance criteria above.

Critic: Multi-model adversarial review after implementation, with Claude excluded as critic if Claude Code is the Builder.

Reporter: Claude project session, producing the Elevated Build-Run Report after implementation.

## Reserved-Terms Check

Has this plan been reviewed for SPEC-8 §6.1 reserved-term collisions? YES.

This plan does not redefine reserved runtime terms such as `pipeline`, `dispatch`, `capability`, `promotion`, `demotion`, `governing`, or `auxiliary`.

Phase 4A introduces the following local contract terms for this plan only:

- `Proposal`: structured review artifact surfaced to a human before commitment.
- `Agent Inbox`: client-facing concept represented by jobs in `proposal_ready`; not a new runtime component.
- `proposal_ready`: non-terminal job status awaiting human review.
- `closed_no_action`: terminal-neutral job status for `decline_to_act`.
- `defer`: review decision that keeps the job pending.
- `decline_to_act`: terminal-neutral review decision distinct from `reject`.
- `client_source`: mobile audit-source identity, limited to `phone_app` and `watch_app`.
- `decision_idempotency_key`: client-supplied replay-protection key for review decisions.
- `stale decision`: review request whose `result_id` or `response_hash` no longer matches the current authoritative result.

## Amendments

### 2026-04-26 — Reconcile ReviewRequest (AC #5) with edit-result storage (AC #14)

**Authorized by:** Lawrence Jeffords on 2026-04-26 per `docs/plans/phase-4a-backend-contract.md` commit `d2beec6`.

**Issue resolved (Phase 4A.1 carry-forward):**

- AC #5 originally defined `ReviewRequest` with `decision`, `result_id`, `response_hash`, and `decision_idempotency_key`.
- AC #14 requires the `edit` decision to store a modified result and preserve modified-result lineage.
- `ReviewRequest` uses `extra="forbid"`, so edited content has no legal payload slot and would be rejected as an unknown field.
- `ReviewRequest.decision` is currently typed as a free-form `str` rather than a locked enum, allowing any string value to pass schema validation and pushing the entire decision-vocabulary check to handler logic.

This amendment closes both gaps before any endpoint wiring begins.

**Schema amendments to `ReviewRequest`:**

1. `ReviewRequest` is amended to include:
   - `modified_result: Optional[str] = None`

2. Handler-side validation rule (enforced by the review endpoint, not by the schema):
   - `modified_result` is required if and only if `decision == "edit"`.
   - For all other decisions (`approve`, `reject`, `defer`, `decline_to_act`), `modified_result` must be absent or `null`.
   - Violations of this rule must be rejected by the handler with HTTP `422`.

3. `ReviewRequest.decision` is locked as `ReviewDecision(str, Enum)` with exactly these values, and no others:
   - `approve`
   - `edit`
   - `reject`
   - `defer`
   - `decline_to_act`

   `ReviewDecision` follows the same `(str, Enum)` precedent already established in `orchestrator/models.py` by `InputModality`, `Device`, `ClientSource`, and `JobStatus`.

**Required Phase 4A sequencing (locked):**

1. **Plan amendment** — this commit. Docs-only.
2. **Schema delta** — a separate small commit that updates `orchestrator/models.py` to add the `ReviewDecision` enum, retype `ReviewRequest.decision` as `ReviewDecision`, and add `modified_result: Optional[str] = None`, plus the corresponding additions to `tests/test_phase4a_backend_contract.py`. No endpoint, persistence, audit, or handler logic in this commit.
3. **Endpoint wiring** — third, only after the schema-delta commit lands. Endpoint wiring must not begin until then.

**Effect on existing acceptance criteria:**

- AC #5 is amended to read: `ReviewRequest` exists for `POST /jobs/{job_id}/review`; requires `decision`, `result_id`, `response_hash`, and `decision_idempotency_key`; includes optional `modified_result`; types `decision` as `ReviewDecision`; and uses `extra="forbid"`.
- AC #14 is unchanged in intent. This amendment supplies the explicit legal payload slot through which the edited result is delivered.
- AC #24 (`POST /jobs/{job_id}/review` with an unknown field returns HTTP `422`) is unaffected. `modified_result` becomes a known field; any other field remains unknown.
- All other acceptance criteria remain unchanged.

**Out of scope for this amendment:**

- No endpoint wiring.
- No `job_manager` review-handling behavior.
- No persistence, audit-event, `STATUS.md`, or `docs/SPEC-MAP.md` changes.
- No tests are run or added by this amendment.

### 2026-04-26 — Define proposal population semantics

**Authorized by:** Lawrence Jeffords on 2026-04-26 per `docs/plans/phase-4a-backend-contract.md` commit `dde0de6` and schema delta commit `8f1a2ef`.

**Issue resolved (Phase 4A.2.a):**

The schema now defines `Proposal` (AC #2) and `JobStatus.proposal_ready` (AC #1), but three population-side semantics remain ambiguous and must be locked before the proposal-population implementation slice (Phase 4A.2.b) begins:

1. `proposed_by` provenance is unspecified. Without a definition, implementation could ambiguously source it from the human reviewer identity, the client device, or the producing model.
2. `auto_accept_at` behavior for `proposal_ready` jobs is unspecified. Without a definition, implementation could silently introduce automatic acceptance from `proposal_ready`, which would change human-review authority.
3. The trigger for entering `proposal_ready` is described in scope ("jobs held for human review by the existing review-gate logic") but is not stated as the single locked entry path. Without an explicit trigger, implementation could introduce parallel paths into `proposal_ready` that bypass the existing held / delivery-hold logic.

This amendment closes all three gaps before any proposal-population code is written.

**Locked decisions:**

1. **`proposed_by` provenance.**
   - `proposed_by` is the selected/producing model identifier known at response-generation time.
   - For local proposals, `proposed_by` is the local model string.
   - For cloud proposals, `proposed_by` is the target cloud model string.
   - `proposed_by` identifies the producer of the proposed result. It is not the human reviewer identity and not the client device identity.

2. **`auto_accept_at` v1 behavior for `proposal_ready` jobs.**
   - `auto_accept_at` is `null` for v1 `proposal_ready` jobs.
   - Phase 4A does not introduce automatic acceptance from `proposal_ready`.
   - Any future auto-accept behavior for `proposal_ready` requires a separate plan amendment because it changes human-review authority.

3. **`proposal_ready` trigger.**
   - A job enters `proposal_ready` when the existing review-gate / delivery-hold logic determines that a result must be held for human review before delivery or closure.
   - `proposal_ready` is the client-visible representation of that held result.
   - This preserves the sequence: result produced → proposal recorded → client retrieves proposal → review endpoint later decides outcome.

**Effect on existing acceptance criteria:**

- AC #2 is unchanged in field shape. This amendment defines the runtime meaning of the `proposed_by` field.
- AC #10 is unchanged. The "no auto-accept applies" branch is now the only branch that fires for `proposal_ready` in v1; the absolute-timestamp branch remains defined for future use but is not exercised by Phase 4A `proposal_ready` jobs.
- AC #8 is unchanged. This amendment makes explicit that the held / delivery-hold path is the sole entry into `proposal_ready` for Phase 4A.
- All other acceptance criteria remain unchanged.

**Scope boundary for this amendment:**

- This amendment does not implement code.
- This amendment does not wire `POST /jobs/{job_id}/review`.
- This amendment does not wire `GET /jobs` listing.
- This amendment does not resolve review idempotency ambiguity or wrong-status review behavior; those require a later amendment before the review-handler slice.

**Out of scope for this amendment:**

- No endpoint wiring.
- No `job_manager` proposal-population or review-handling behavior.
- No persistence, audit-event, `STATUS.md`, or `docs/SPEC-MAP.md` changes.
- No tests are run or added by this amendment.

### 2026-04-26 — Define proposal-ready audit event and triggers

**Authorized by:** Lawrence Jeffords on 2026-04-26 per `docs/plans/phase-4a-backend-contract.md` commit `8377884` and implementation discovery report.

**Issue resolved (Phase 4A.2.b):**

The 2026-04-26 amendment "Define proposal population semantics" locked `proposed_by` provenance, `auto_accept_at` v1 behavior, and the high-level `proposal_ready` entry path, but three implementation-discovery findings remain ambiguous and must be locked before the proposal-population implementation slice (Phase 4A.2.b) begins:

1. Entering `proposal_ready` is a lifecycle state transition but the plan does not define a durable audit event that records that transition. Without one, the held-result decision is not auditable until a later `human.reviewed` event fires, which can be never (for example, when a job remains in `proposal_ready` indefinitely under repeated `defer`).
2. The existing held / delivery-hold logic in `orchestrator/job_manager.py` includes `pre_action` and `cost_approval` hold types that fire before a result artifact exists, alongside `on_accept` (post-result) holds and a `delivery_hold` flag on `wal.permission_check`. The plan does not distinguish result-holding from pre-action / cost-approval holds. A literal reading of "the held / delivery-hold path is the sole entry into `proposal_ready`" could treat all `held` permission checks as `proposal_ready` triggers, including ones with no result to propose.
3. Proposal derivation is implicitly required to populate `JobStatusResponse.proposal` but is not located. Without a locked location, implementation could duplicate proposal-derivation rules in the HTTP route layer instead of centralizing them in `JobManager`.

This amendment closes all three gaps before any proposal-population code is written.

**Locked decisions:**

1. **Durable `job.proposal_ready` audit event.**
   - Entering `proposal_ready` emits a durable lifecycle event of type `job.proposal_ready`.
   - It is emitted after the result/response artifact has been created and recorded, and before the pipeline stops for human review.
   - It is not a `human.reviewed` event and does not imply acceptance, rejection, delivery, or closure.
   - It marks the held-result decision in the audit log so that pending review state is durably attributable even when a `human.reviewed` event is never emitted (for example, indefinite `defer` or operator silence).

2. **`job.proposal_ready` payload.**
   The payload includes at minimum:
   - `proposal_id`
   - `result_id`
   - `response_hash`
   - `proposed_by`
   - `governing_capability_id`
   - `confidence`
   - `auto_accept_at`
   - `hold_reason`

   `hold_reason` values for Phase 4A.2.b are exactly:
   - `pre_delivery`
   - `on_accept`

3. **`proposal_ready` trigger.**
   - `proposal_ready` applies only when a result exists and must be held for human review.
   - `delivery_hold == true` on a `wal.permission_check` is an authorized `proposal_ready` trigger.
   - `result == "held"` with `hold_type == "on_accept"` is an authorized `proposal_ready` trigger if a result exists.
   - `pre_action` and `cost_approval` holds are not `proposal_ready` triggers because no result artifact exists yet at the point those holds fire.
   - `blocked` permission checks remain failure / blocked behavior and do not become proposals.

4. **`proposed_by` local route.**
   - For local proposals, the literal producing local model tag is the `proposed_by` value.
   - The current local value may be `candidate_models[0]`, for example `"llama3.1:8b"`.
   - Historical proposals retain the model string used at proposal time; no later normalization or current-model lookup is performed.

5. **Proposal derivation location.**
   - Proposal construction should be centralized in `JobManager` or an equivalent non-route helper.
   - The `GET /jobs/{job_id}` route may call that helper and pass the returned `Proposal` into `JobStatusResponse`.
   - The HTTP route should not duplicate proposal-derivation rules.

**Effect on existing acceptance criteria:**

- AC #2 is unchanged in field shape. This amendment defines a separate durable lifecycle event (`job.proposal_ready`) that records the transition into `proposal_ready` and carries proposal lineage onto the audit log; the `Proposal` model surfaced on `JobStatusResponse` is unchanged.
- AC #8 is unchanged in intent. This amendment narrows the held-path entry into `proposal_ready` to the result-holding subset (post-result `on_accept` holds and `delivery_hold == true`) and explicitly excludes `pre_action` and `cost_approval` holds from being proposal triggers.
- AC #9 is unchanged. This amendment locates proposal derivation in `JobManager` (or an equivalent non-route helper) so the `GET /jobs/{job_id}` route obtains the `Proposal` from that helper rather than constructing it inline.
- The 2026-04-26 amendment "Define proposal population semantics" is unchanged. This amendment supplements its `proposed_by` provenance rule by stating that for local proposals the producing model tag is used as the literal `proposed_by` value.
- All other acceptance criteria remain unchanged.

**Scope boundary for this amendment:**

- This amendment does not implement code.
- This amendment does not wire `POST /jobs/{job_id}/review`.
- This amendment does not wire `GET /jobs` listing.
- This amendment does not resolve review idempotency ambiguity or wrong-status review behavior; those require a later amendment before the review-handler slice.

**Out of scope for this amendment:**

- No endpoint wiring.
- No `job_manager` proposal-population, proposal-event-emission, or review-handling behavior.
- No persistence, audit-event implementation, `STATUS.md`, or `docs/SPEC-MAP.md` changes.
- No tests are run or added by this amendment.

### 2026-04-26 — Define review handler conflict and serialization semantics

**Authorized by:** Lawrence Jeffords on 2026-04-26 per `docs/plans/phase-4a-backend-contract.md` commit `eff74ee` and the Phase 4A.2 review-concurrency discovery report.

**Issue resolved (Phase 4A.2.c):**

The Phase 4A.2.b amendment "Define proposal-ready audit event and triggers" landed `job.proposal_ready` lifecycle events, hold-reason vocabulary, and proposal-derivation location. Six review-handler ambiguities remain and must be locked before `POST /jobs/{job_id}/review` is implemented:

1. The plan does not define HTTP behavior when a review request targets a job that is not in `proposal_ready`. Without an explicit rule, implementation could ambiguously return 404, 409, or silently no-op against an already-resolved job.
2. The stale-decision check (AC #18, #19) is defined for mismatched `result_id` / `response_hash`, but the plan does not specify whether stale-decision evaluation runs before or after `decision_idempotency_key` replay, nor whether stale-decision checks must run before any state mutation or durable review event.
3. `decision_idempotency_key` semantics are defined for replay (AC #20, #21) but not for same-key/different-payload, different-key/same-payload, or the namespaced key form. Without explicit rules, replay could silently overwrite a prior decision or accept a different decision under the same key.
4. The Phase 4A.1 carry-forward amendment defines the `modified_result` shape and the conditional rule that `modified_result` is required iff `decision == "edit"`, but does not state the HTTP error code on violation.
5. The `human.reviewed` decision vocabulary is currently described in AC #13–#17 using the older `accepted | modified | rejected | deferred | declined` strings. The Phase 4A.1 carry-forward amendment locked `ReviewDecision(str, Enum)` as `approve | edit | reject | defer | decline_to_act`. The audit-side decision vocabulary for the review endpoint is therefore ambiguous: implementation could either translate review decisions back to the older strings or carry the `ReviewDecision` values straight through.
6. The plan does not define how `POST /jobs/{job_id}/review` and `POST /jobs/{job_id}/override` serialize against the same `proposal_ready` job. Without an explicit rule, a concurrent review and override could each mutate the job, double-emit terminal events, or race past stale-decision matching by relying solely on `result_id` / `response_hash` equality.

This amendment closes all six gaps before any review-handler code is written.

**Locked decisions:**

1. **Reviewable-state conflict.**
   - `POST /jobs/{job_id}/review` is valid only when the target job is in `proposal_ready`.
   - A review request for any other job status returns HTTP `409 Conflict`.
   - The 409 response body must include:
     - `error`
     - `current_status`
     - `current_result_id`
     - `current_response_hash`
     - `message`

2. **Stale-decision conflict.**
   - `ReviewRequest.result_id` and `ReviewRequest.response_hash` must both match the current authoritative proposal.
   - If either value mismatches, the handler returns HTTP `409 Conflict`.
   - The 409 response body must include:
     - `error`
     - `current_status`
     - `current_result_id`
     - `current_response_hash`
     - `message`
   - Order of checks:
     - If the `decision_idempotency_key` is already known and maps to a stored review outcome, idempotency replay is evaluated *before* stale-decision checks.
     - If the `decision_idempotency_key` is not known, stale-decision checks are evaluated *before* any state mutation or durable review event.

3. **`decision_idempotency_key` semantics.**
   - **Same key + same payload:** return the original stored review outcome without repeating state transitions and without emitting duplicate durable decision events.
   - **Same key + different payload:** return HTTP `409 Conflict`. Do not apply the new decision. Do not emit a new `human.reviewed` event.
   - **Different key + same payload:** treat as a new review attempt. If the job is no longer in `proposal_ready` because a prior decision already resolved it, the wrong-status 409 from rule (1) applies.
   - **Idempotency-key namespace:** keys are stored under the prefix `review_idem:{decision_idempotency_key}`, distinct from the existing submit-side idempotency namespace.

4. **Review payload identity for idempotency comparison.**
   The payload identity used to detect "same payload vs. different payload" under the same `decision_idempotency_key` includes:
   - `job_id`
   - `decision`
   - `result_id`
   - `response_hash`
   - `modified_result`
   - `decision_idempotency_key`

5. **Decision outcomes.**
   - `approve`: accepts the proposed result and transitions the job to `delivered`.
   - `edit`: requires `modified_result`; stores the modified result as a new result artifact, updates the authoritative `result_id` / `response_hash` to the modified artifact, and transitions the job to `delivered`.
   - `reject`: rejects the proposed result and transitions the job to `failed`. The existing plan does not define a more precise rejected terminal state for Phase 4A; if a future amendment introduces one, the more precise terminal state takes precedence.
   - `defer`: records a non-terminal review/defer outcome and leaves the job in `proposal_ready`. After `defer`, the job remains reviewable. A later review attempt with a fresh `decision_idempotency_key` is permitted.
   - `decline_to_act`: transitions the job to `closed_no_action`.

6. **`modified_result` handler validation.**
   - `modified_result` is required iff `decision == "edit"`.
   - `modified_result` must be absent or `null` for `approve`, `reject`, `defer`, and `decline_to_act`.
   - Violations return HTTP `422`.

7. **`human.reviewed` event decision vocabulary for the review endpoint.**
   - For `POST /jobs/{job_id}/review`, the emitted `human.reviewed` event carries the same decision vocabulary as `ReviewDecision`:
     - `approve`
     - `edit`
     - `reject`
     - `defer`
     - `decline_to_act`
   - Review endpoint decisions must not be translated to the older `accepted | modified | rejected | resubmitted` vocabulary.
   - The audit record preserves the exact `ReviewDecision` value supplied by the endpoint.
   - The pre-existing `human.reviewed` decision strings (`modified`, `auto_delivered`, etc.) emitted from `override_job` and the auto-accept loop are unchanged by this amendment; this rule governs only the review endpoint emit path.

8. **Review-vs-override serialization for `proposal_ready` jobs.**
   - `POST /jobs/{job_id}/review` and `POST /jobs/{job_id}/override` are mutually exclusive mutation surfaces for a `proposal_ready` job.
   - First committed decision wins.
   - If review commits first, a later override returns the existing override no-op form (`already_overridden` or equivalent) and must not mutate the job.
   - If override commits first, a later review returns HTTP `409 Conflict` using the wrong-status / current-status response body defined in rule (1).
   - The winning path must mark the job as no longer reviewable, or otherwise set its first-writer guard, *before* any durable audit await that could interleave with another mutation.
   - The review handler must not rely solely on stale `result_id` / `response_hash` matching; it must also check reviewable status and the first-writer guard.
   - `cancel`, `redirect`, and `escalate` overrides remain allowed against `proposal_ready` jobs before a review wins, consistent with the existing override-wins doctrine.
   - Once any review or override decision wins, subsequent review/override attempts are blocked or no-op according to the rules above.

9. **Implementation-order guard.**
   - The review handler must set its first-writer guard before emitting durable `human.reviewed`, `job.delivered`, `job.failed`, or `job.closed_no_action` events.
   - Override handling for `proposal_ready` jobs must set its first-writer guard before emitting durable `human.override` or terminal events.
   - This amendment does not require introducing `asyncio.Lock` in Phase 4A.2.c; ordering and state guards are sufficient for this slice unless implementation discovers otherwise and stops for a new plan amendment.

10. **Scope boundary.**
    - This amendment does not implement code.
    - This amendment does not wire `POST /jobs/{job_id}/review`.
    - This amendment does not wire `GET /jobs` listing.
    - This amendment does not update `STATUS.md` or `docs/SPEC-MAP.md`.
    - Confidence default for successor jobs is out of scope for this amendment and remains a later implementation review item.

**Effect on existing acceptance criteria:**

- AC #13 is amended: review decision `approve` transitions a `proposal_ready` job to `delivered` and emits `human.reviewed` with `decision="approve"` (not `"accepted"`).
- AC #14 is unchanged in intent. The emitted `human.reviewed` for `edit` carries `decision="edit"` (not `"modified"`); modified-result lineage is preserved as already specified, with the `result_id` / `response_hash` of the modified artifact becoming the authoritative result identity per rule (5).
- AC #15 is amended: review decision `reject` transitions a `proposal_ready` job to `failed` and emits `human.reviewed` with `decision="reject"` (not `"rejected"`).
- AC #16 is amended: review decision `defer` leaves the job in `proposal_ready`, emits `human.reviewed` with `decision="defer"` (not `"deferred"`), and does not increment promotion or demotion counters.
- AC #17 is amended: review decision `decline_to_act` transitions the job to `closed_no_action`, emits `human.reviewed` with `decision="decline_to_act"` (not `"declined"`), and does not increment promotion or demotion counters.
- AC #18 and AC #19 are unchanged in intent. The 409 response body fields (`error`, `current_result_id`, `current_response_hash`, `current_status`, `message`) are unchanged. This amendment additionally locks the wrong-status 409 from rule (1) using the same response body shape.
- AC #20 and AC #21 are unchanged in intent. This amendment additionally locks the same-key/different-payload 409 conflict, the different-key handling, the `review_idem:` namespace, and the order of idempotency-replay versus stale-decision checks.
- AC #24 is unchanged. The 422 response for unknown request fields is unaffected; the new 422 surface introduced by rule (6) is for the `modified_result` conditional handler-side rule, not for schema-side `extra="forbid"` validation.
- AC #30 is amended: the closed list of `human.reviewed` decision values emitted by the review endpoint for Phase 4A is `approve | edit | reject | defer | decline_to_act`. The pre-existing `human.reviewed` decision values (`accepted`, `modified`, `auto_delivered`, etc.) emitted from `override_job` and the auto-accept loop are preserved unchanged for those code paths and are not new values added by this amendment.
- All other acceptance criteria remain unchanged.

**Scope boundary for this amendment:**

- This amendment does not implement code.
- This amendment does not wire `POST /jobs/{job_id}/review`.
- This amendment does not wire `GET /jobs` listing.
- This amendment does not modify the schema (the Phase 4A.1 carry-forward amendment already retyped `ReviewRequest.decision` as `ReviewDecision` and added `modified_result`).
- This amendment does not change `STATUS.md` or `docs/SPEC-MAP.md`.

**Out of scope for this amendment:**

- No endpoint wiring.
- No `job_manager` review-handling, idempotency-namespace, first-writer-guard, or serialization implementation.
- No persistence, audit-event implementation, `STATUS.md`, or `docs/SPEC-MAP.md` changes.
- No tests are run or added by this amendment.
