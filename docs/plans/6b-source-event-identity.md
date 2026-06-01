# Plan: §6B Source-Event Identity — Normalization + Interim Conflict Contract

**Status:** Contract defined; implementation slice = §6B-1 + §6B-2 (interim). §6B-3 deferred.
**Date:** 2026-06-01
**Evidence basis:** [`DRNT_Week1_Phase4A2_Evidence/current_truth_report.md`](../../DRNT_Week1_Phase4A2_Evidence/current_truth_report.md)
(HEAD `2d035e3`, `v0.2.1-34-g2d035e3`). Read that report first — this plan closes the gap it documents.

## Objective

Give a mobile **source event** a **durable, intent-aware identity** so that re-sending the
same source event is deduplicated (not re-executed) *and* a source event re-sent with
**changed intent** is blocked with an explicit interim conflict instead of silently creating a
second executing job.

This slice delivers:

- **§6B-1** — persist source-event identity `(client_source, client_source_event_id)` and an
  **intent-equivalence hash** computed over a deterministic normalized form of `raw_input`.
- **§6B-2** — equivalent-replay **dedup** and changed-intent **`409 Conflict`** with a
  structured body and a conflict audit event. This is an **interim** result, valid only until
  §6B-3 (human reconfirmation) exists.

## Background — what the current-truth report establishes

Three request identities exist today; none owns source-event replay (report STEP 1):

| Identity | Persisted | Survives terminal + restart | Dedups source-event replay |
|---|---|---|---|
| `idempotency_key` (submit) | yes (`idem:` ns + `jobs` col) | **no** — purged for terminal jobs on restart | only while live |
| `decision_idempotency_key` (review) | yes (`review_idem:` ns) | yes (never purged) | no (guards the review POST) |
| `client_source_event_id` (source event) | **no** — accepted then dropped (`main.py:407-412`) | n/a | **none** |

Decision-relevant consequences the report proved live (all local, zero cloud):

1. Submit idempotency is **purged the moment a job is terminal and the orchestrator restarts**
   (`idempotency_store._purge_terminal_keys`). A mobile client that re-sends the same source
   event after delivery + a hub restart creates a **duplicate job** (`CC-R6`). Source-event
   identity needs durability *past* terminal state; submit idempotency deliberately does not.
2. Review idempotency is durable but scoped to the review decision, not the source event.
3. There is **no third identity and no §6 replay guard** today — `client_source_event_id` is
   dropped before persistence and before audit.
4. The Phase 4A contract intended preservation (AC#6: `job.submitted` preserves the client
   values) but only "accept-then-drop" shipped; AC#6 has **no preservation code and no test**.

§6B closes (1), (3), and (4) and adds the changed-intent guard.

### Numbering / naming caution (read before building)

The `§5 / §6B-1/2/3 / §7.4 / §11` numbers are the **Source-Event Identity ADR's** internal
sections. They are **not** the repo's canonical Spec-1..8 (see [`docs/SPEC-MAP.md`](../SPEC-MAP.md)).
In repo terms this work extends the **request-identity / idempotency** surface (adjacent to
Spec 7B Signal-Chain idempotency) and the **Phase 4A mobile contract**.

**Collision to avoid:** the repo already has `tests/test_phase6b.py` for **Spec 6 phase 6b
(Silo Runtime Security)** — entirely unrelated. New code and tests for this ADR slice use the
`source_event*` / `test_source_event_identity` names, never `phase6b`.

## Scope

In scope (this slice):

1. **Normalization + intent hash** (§5.6 / Insert A) — a pure, unit-testable function pair.
2. **Durable source-event store** — a new `srcevent:` namespace in the existing `state` table,
   keyed by `(client_source, client_source_event_id)`, holding `{job_id, intent_equivalence_hash}`.
   **Durable like `review_idem:`; never purged** (this is the durability fix).
3. **Lineage persistence** — carry `client_source`, `client_source_event_id`, `client_timestamp`,
   and `intent_equivalence_hash` onto the `Job` record and into the `job.submitted` **payload**
   (closes 4A AC#6 and the report's STEP 5 lineage gap).
4. **Equivalent-replay dedup** — same source event + same intent hash → return the **original**
   job, no duplicate, no re-classification, **durable across terminal + restart**.
5. **Changed-intent `409`** (§7.4 / Insert B) — same source event + different intent hash →
   `409 Conflict` + structured body + durable conflict audit event; no duplicate job, original
   mapping preserved.
6. **Acceptance tests** (§11.8 / §11.9 / Insert C) and **cost-aware test buckets** (Insert D).

## Out of Scope

- **§6B-3 human reconfirmation / reviewable-ambiguity hold.** The `409` is explicitly interim;
  `adr_compliance = partial_until_reconfirmation_path_exists`. Final ADR compliance requires §6B-3.
- **Envelope-level source injection.** We do **not** override the audit envelope's
  server-generated `source_event_id` / `timestamp` / `source` (doing so would collide with the
  audit-log-writer's envelope dedup `_seen_source_ids`). Lineage lives in the event **payload**.
- **Case / punctuation normalization** beyond §5.6. Case and punctuation are **preserved** this
  slice (rationale below); any domain-specific rule is a later ADR.
- Any iOS / Swift / WatchConnectivity / APNs client code.
- Any `audit-log-writer` source change (it already accepts new event types).

## Normative contract

### §5.6 Raw-input normalization (Insert A)

The intent-equivalence hash is computed over a deterministic **normalized** form of
`raw_input`, not the raw bytes:

```text
normalized_raw_input =
    Unicode NFC normalization
    + trim leading/trailing whitespace
    + collapse internal whitespace runs to a single ASCII space
    + preserve case
    + preserve punctuation
```

Equivalent: `"  remind   me to call John  "` ≡ `"remind me to call John"`.
Non-equivalent: `"remind me to call John"` ≠ `"remind me to text John"`.

**Case** is preserved (may be semantically meaningful in names, codes, paths, acronyms, IDs).
**Punctuation** is preserved (can affect meaning in structured commands, lists, filenames,
measurements, quoted text). If punctuation/case jitter becomes a practical problem, add a later
tested ADR rule rather than guessing now.

### Required hash inputs (Insert A)

```text
intent_equivalence_hash = hash({
    "normalized_raw_input": normalized_raw_input,
    "input_modality": input_modality,
})
```

Hash = SHA-256 over a canonical JSON serialization (sorted keys, no insignificant whitespace).
**Do not include** `device`, `client_timestamp`, `idempotency_key`, `client_source`,
`client_source_event_id`, `app_version`, `session_id`, transport/serialization metadata, retry
count, or non-user-facing capability hints. (`input_modality` is included unless a later ADR
says otherwise; `client_source` keys the *mapping* but is not in the *intent* hash.)

### §7.4 Changed-intent interim contract (Insert B)

Changed-intent replay = **same** `client_source + client_source_event_id`, **different**
`intent_equivalence_hash`. The submit endpoint **must not** return `202` on this path.

Response: **`HTTP 409 Conflict`** with a body conveying the semantics of:

```json
{
  "source_event_replay": true,
  "source_event_conflict": true,
  "conflict_type": "intent_mismatch",
  "original_job_id": "<existing job id>",
  "action_required": "reconfirmation_required",
  "adr_compliance": "partial_until_reconfirmation_path_exists",
  "message": "Same source event identity was replayed with changed intent. Duplicate execution was blocked. Human reconfirmation path is required before changed intent can be acted on."
}
```

Required side effects on changed-intent replay:

- ☐ Do **not** create a duplicate executing job.
- ☐ Do **not** enqueue classification/generation for the changed intent.
- ☐ Do **not** overwrite the original source-event mapping.
- ☐ Do **not** silently discard the changed intent as final behavior.
- ☐ Emit a conflict/ambiguity audit event.
- ☐ Return the explicit `409` body above.
- ☐ Mark reconfirmation (§6B-3) as the required follow-on.

**Boundary:** `409 + structured body + audit event` is acceptable as the **interim §6B-2**
result only. Final ADR compliance requires the §6B-3 human-reconfirmation hold.

## Design decisions (repo mapping)

- **D1 — Durable third identity.** Add a `srcevent:` namespace to the `state` table via
  `IdempotencyStore` (same machinery that already holds `idem:` and `review_idem:`). It is
  loaded on startup and **excluded from `_purge_terminal_keys`** (which matches only `idem:`).
  Key = `srcevent:{client_source}:{client_source_event_id}`; value =
  `{job_id, intent_equivalence_hash, client_source, client_source_event_id, created_at}`.
  Collision-free because `client_source` is a closed enum (`phone_app` / `watch_app`).
- **D2 — Authoritative precedence.** When both `client_source` and `client_source_event_id`
  are present, the source-event identity is evaluated **first and atomically**, ahead of the
  `idempotency_key` dedup. With no source-event identity, behavior is unchanged
  (`idempotency_key` only). The store check returns one of `new | equivalent | conflict`.
- **D3 — Equivalent replay returns the original job even when terminal.** Because the mapping
  is durable but terminal jobs are not reloaded into `JobManager._jobs`, the equivalent-replay
  path point-reads the `jobs` table for the original `job_id` and returns it. No new job, no
  re-classification.
- **D4 — Lineage in payload, not envelope.** `event_job_submitted` gains `client_source`,
  `client_source_event_id`, `client_timestamp`, and `intent_equivalence_hash` in its payload;
  the envelope `source_event_id` / `timestamp` / `source` stay server-generated (avoids
  audit-dedup collisions). Closes 4A AC#6 and the report's client-origin lineage gap.
- **D5 — Conflict signalled by exception.** `JobManager.submit_job` raises
  `SourceEventIntentConflict(body)` after emitting the durable conflict event; `main.py`
  catches it ahead of its broad `except` and returns `JSONResponse(409, body)`.
- **D6 — Hash & normalization live in a pure module** (`orchestrator/source_event.py`) so the
  free/unit tests need neither the stack nor the model.

## Files Likely Affected

| File | Change |
|---|---|
| `orchestrator/source_event.py` | **new** — `normalize_raw_input`, `compute_intent_equivalence_hash`, `source_event_key` |
| `orchestrator/idempotency_store.py` | `srcevent:` namespace: `SourceEventRecord`, `check_and_store_source_event` (`new`/`equivalent`/`conflict`), `get_source_event`, startup load; **not** purged |
| `orchestrator/models.py` | `Job` gains `client_source`, `client_source_event_id`, `client_timestamp`, `intent_equivalence_hash` (auto-persist via `asdict` / `Job(**data)`) |
| `orchestrator/events.py` | extend `event_job_submitted` payload (lineage + hash); add `event_source_event_conflict` |
| `orchestrator/job_manager.py` | `submit_job` gains client params + source-event guard; `SourceEventIntentConflict`; terminal-job point-read helper |
| `orchestrator/main.py` | forward client fields; map `SourceEventIntentConflict` → `409` |
| `orchestrator/test_source_event.py` | **new** — normalization + hash unit matrix (free) |
| `tests/test_source_event_identity.py` | **new** — submit-path behavior (free bucket) + local-model bucket |
| `STATUS.md`, `docs/SPEC-MAP.md` | claim rows + spec-to-impl mapping for §6B |

## Risks

- **R1 — Concurrency race on first reservation.** Two truly-simultaneous submits of the same
  source event with *different* idempotency keys: mitigated by making
  `check_and_store_source_event` atomic under the store lock (first writer wins; the loser sees
  `equivalent` or `conflict`). Note residual window vs. the cross-process case (single
  orchestrator process today, so in-process lock suffices).
- **R2 — `Job(**data)` forward-compat.** Adding fields is safe (old rows lack the keys →
  dataclass defaults apply); removing/renaming would break reload. This slice only **adds**.
- **R3 — Hash stability.** The hash is a durable identity; its definition (normalization +
  inputs + canonical JSON + SHA-256) is now contract. Any future change is a breaking ADR.
- **R4 — Interim 409 mistaken for final.** The body and tests must state
  `partial_until_reconfirmation_path_exists` and that §6B-3 is required; otherwise a reader may
  treat `409` as final ADR compliance.

## Acceptance Criteria

1. [file-state-verifiable] `orchestrator/source_event.py` defines `normalize_raw_input` and `compute_intent_equivalence_hash`; the hash is taken over exactly `{normalized_raw_input, input_modality}`.
2. [test-verifiable] Whitespace-only and leading/trailing-whitespace differences in `raw_input` do **not** change the hash.
3. [test-verifiable] Unicode-equivalent strings (NFC) normalize to the **same** hash.
4. [test-verifiable] Changing `device` or `client_timestamp` does **not** change the hash.
5. [test-verifiable] A meaningful `raw_input` change (`call`→`text`) **does** change the hash; changing `input_modality` changes the hash.
6. [file-state-verifiable] The `srcevent:` namespace is loaded on startup and is **not** matched by `_purge_terminal_keys` (durable across terminal + restart).
7. [test-verifiable] Submitting a source event persists `client_source`, `client_source_event_id`, `client_timestamp`, and `intent_equivalence_hash` on the `Job`, and the emitted `job.submitted` payload preserves them (4A AC#6).
8. [test-verifiable] Backward-compat: a submit with **no** client fields still produces a `job.submitted` with `source="orchestrator"`, server UUIDv7 `source_event_id`, server ISO timestamp, and no source-event mapping.
9. [test-verifiable] Equivalent replay (same source event, same normalized intent) returns the **original** `job_id`, creates **no** duplicate job, and enqueues **no** classification — including after the original job is terminal and the store has been reloaded (restart).
10. [behavior-verifiable] Changed-intent replay returns **`409 Conflict`** (never `202`) with a body conveying `source_event_replay=true`, `source_event_conflict=true`, `conflict_type=intent_mismatch`, `original_job_id`, `action_required=reconfirmation_required`, and `partial_until_reconfirmation_path_exists`.
11. [test-verifiable] On changed-intent replay: no duplicate executing job is created, the original source-event mapping is unchanged, and a conflict audit event is emitted.
12. [test-verifiable] Tests are split into the cost-aware buckets of Insert D; the **free** bucket runs in-process (no model, no live stack) and passes first.
13. [file-state-verifiable] No `phase6b` name is reused; no `audit-log-writer` source is modified; no envelope `source_event_id`/`timestamp`/`source` override is introduced.
14. [file-state-verifiable] `STATUS.md` and `docs/SPEC-MAP.md` updated with §6B rows.
15. [test-verifiable] Pre-existing tests for `POST /jobs`, idempotency, persistence, and audit continue to pass.

## §11 Acceptance tests (Insert C, verbatim intent)

**11.8 Normalization**
- ☐ Submit `raw_input=" remind me to call John "`; replay with `"remind me to call John"`; confirm same hash, no duplicate job, replay/dedup.
- ☐ Replay with `"remind me to text John"`; confirm different hash, changed-intent conflict, `409`, conflict audit event, no duplicate executing job.

**11.9 Interim 409 contract**
- ☐ Submit; replay same `client_source + client_source_event_id` with changed normalized `raw_input`.
- ☐ Confirm `409`; body conveys `source_event_replay`, `source_event_conflict`,
  `conflict_type=intent_mismatch`, `original_job_id`, `action_required=reconfirmation_required`,
  `partial_until_reconfirmation_path_exists`.
- ☐ Confirm changed intent is **not** accepted with `202`; no duplicate executing job; audit
  records the conflict; test notes final compliance requires §6B-3.

## Cost-aware test execution (Insert D)

Source-event identity tests run **before** model-backed lifecycle tests.

**Free / submit-boundary (run first; no job completion, no model):**
`6B-1` persistence, intent-hash, equivalent replay *before* classification, envelope-drift
replay, changed-intent `409`, `client_timestamp` lineage-only. Implemented in-process with
`MockAuditClient` + temp SQLite; runnable with `pytest --noconftest` (the `tests/conftest.py`
session gate skips on a down stack).

**Local-model (run after free passes; reach `proposal_ready`/`delivered`):**
equivalent replay *after* `proposal_ready`, *after* delivery, *after* restart with a completed
terminal job. Cost profile **local-only** (`llama3.1:8b` via `drnt-ollama`), **no cloud** — the
exact profile of the prior truth run. Do not use cloud routes for §6B proof unless explicitly
approved.

## Human Approval Required

YES (SPEC-8 Elevated): changes the gateway API contract (submit response can now be `409`), the
job schema, audit lineage, and introduces a durable identity. The user has approved the
plan→implement sequence for this session. Approval covers §6B-1 + §6B-2 (interim) only; it does
**not** authorize §6B-3 or any mobile-client code.

## Reserved-Terms Check

Reviewed for SPEC-8 §6.1 collisions: YES. This plan does not redefine `pipeline`, `dispatch`,
`capability`, `promotion`, `demotion`, `governing`, or `auxiliary`. Local contract terms
introduced for this slice only:

- **source event** — a single mobile-origin submission identified by `(client_source, client_source_event_id)`.
- **source-event identity** — the durable `srcevent:` mapping; the "third identity" of the report.
- **intent-equivalence hash** — SHA-256 over `{normalized_raw_input, input_modality}`.
- **normalized_raw_input** — the §5.6 deterministic form (NFC + trim + collapse whitespace; case/punctuation preserved).
- **equivalent replay** — same source event + same intent hash → dedup.
- **changed-intent replay** — same source event + different intent hash → interim `409`.
- **interim §6B-2** — `409 + body + audit`, valid only until §6B-3 reconfirmation exists.

Naming caution recorded above: this **§6B** is the Source-Event Identity ADR, **not** the
repo's Spec-6 `phase6b` (Silo Runtime Security).

## Amendments

### 2026-06-01 — Concurrency hardening (srcevent atomicity + await-gap)

Review found that the first implementation reserved the `srcevent:` mapping with
last-writer `INSERT OR REPLACE` and committed it *before* the job was durably materialized
(an `await emit_durable` gap). Two reachable failures: (a) an audit-unavailable submit
followed by a retry, and (b) two concurrent identical submits, could both land in the
"mapping exists but job not loadable" branch, which **fell through to create a duplicate job**
and left the mapping dangling. R1's "in-process lock is sufficient" assumption was too narrow.

Corrections landed (no change to §6B-3 / `proposal_ready` / `Proposal` / `review_job` / inbox):

- **Layer 1 — DB-arbitrated first-writer.** `check_and_store_source_event` now reserves via
  `INSERT … ON CONFLICT(key) DO NOTHING` (win/lose by `total_changes` delta; on lose, read the
  canonical DB row and compare hashes). Added `release_source_event` (compare-and-delete
  rollback). Safe should the deployment ever become multi-process.
- **Layer 2 — close the await gap.** A per-source-event `asyncio.Lock` (`_source_event_lock`)
  serializes same-key submits across the whole materialization path; the shared tail is
  extracted into `_create_job`; a `new` reservation is rolled back if the job fails before it
  is durable (audit down), so a retry is not poisoned.
- **Fall-through prohibited.** The unresolvable-equivalent case now briefly polls
  (`_await_job_loadable`), emits a best-effort `source_event.consistency_pending` diagnostic,
  and **fails closed** with `SourceEventConsistencyPending` → retryable `503` — it never creates
  a second job. The invariant "an existing source-event mapping blocks duplicate execution" is
  the non-negotiable contract.

New regression tests (free bucket): concurrent identical submits → one job; audit-failure
rollback → clean retry; unresolvable-equivalent → fail closed; route `503`; DB first-writer and
release; **two-store-instance cross-process first-writer guard** (separate `IdempotencyStore`
objects sharing one SQLite file — proves the `ON CONFLICT` guard, no shared memory). Free bucket
now 33 tests (was 26); full in-process regression (orchestrator/ + in-process tests/, excluding
the 3 live-stack e2e files) 868 passed, 0 failed (re-verified 2026-06-01).

Residual (documented, not required for the single-process deployment): full cross-process
*job* dedup — as opposed to *mapping* integrity — would need reserve-before-materialize across
processes. Out of scope while the orchestrator runs single-process (`uvicorn`, no `--workers`).
