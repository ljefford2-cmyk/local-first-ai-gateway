# DRNT Week 1 — Current-Truth Report for the Source-Event Identity ADR

**Task type:** Observation-and-report only. No repository code was modified. No fix was
implemented. The §6 source-event replay guard was **not** touched (it does not exist in
code — see §1/§0 below).
**Date:** 2026-06-01
**Repo:** `C:\Users\ljeff\drnt-project` · branch `main` · HEAD `2d035e3` · `git describe` = `v0.2.1-34-g2d035e3`
**Report author scope:** Evidence for a human/panel ADR decision. Where ground truth could
not be established, it is marked `CANNOT-DETERMINE` (see final lists).

---

## ⚠️ READ-FIRST CAVEAT: the running stack was STALE relative to the repo

The single most important environmental fact in this report:

- The orchestrator container that was running on arrival was built **2026-04-18/19**
  (`docker image inspect` → `image_created=2026-04-18T18:22:47Z`).
- The Phase 4A backend contract (client fields, `proposal_ready`, review endpoint,
  `decision_idempotency_key`) first landed in commit **`7bd5df5` dated 2026-04-26** — eight
  days *after* that image was built.
- Proof the running image predated Phase 4A: inside the on-arrival container,
  `/app/models.py` contained **none** of `client_source_event_id`, `proposal_ready`,
  `closed_no_action`, `decision_idempotency_key`; `/app/job_manager.py` had **no**
  `proposal_ready` / `_derive_proposal_hold_reason` / `event_job_proposal_ready`.
  Its `JobSubmitRequest` had only `raw_input, input_modality, device, idempotency_key`
  and **no `extra="forbid"`**, so it *silently ignored* the client fields I sent.

**Consequence and what I did about it:** code-reading (Steps 1–2) reflects the **repo at
HEAD**, but live runs against the on-arrival image would reflect **pre-Phase-4A** behavior.
To produce faithful current-code live evidence, I **rebuilt only the orchestrator image
from the repo at HEAD** (`docker compose build orchestrator`; a disposable-runtime/redeploy
action, not a repo-code change) and recreated the container. I verified the new container
carries the Phase 4A surface before re-running. All "current-code" live evidence below
(prefixed `CC_…`) is from the rebuilt image. The two initial stale-image submissions
(`S1`, `R2`) are retained and labeled; they still corroborate the core finding (client
event id dropped — in fact dropped *harder*, since the old model didn't even declare it).

This staleness is itself a finding the panel should note: **the deployed artifact did not
match the source the ADR is reviewing.**

---

## INPUTS FOR ADR DECISION — the three-ID ontology, concretely

The ADR must decide whether **mobile source-event replay** belongs to *submit idempotency*,
*review idempotency*, or a **third durable source-event identity**. The concrete facts:

| Property | `idempotency_key` (submit) | `decision_idempotency_key` (review) | `client_source_event_id` (mobile source event) |
|---|---|---|---|
| Defined | `models.py:69` (req), `:150` (Job) | `models.py:87` (req, **required**) | `models.py:71` (req only; **not** on `Job`) |
| Accepted at boundary | yes | yes | **yes** (declared, `extra="forbid"` → 202; unknown field → 422) |
| Reaches job/pipeline path | yes (`main.py:411`) | yes (`main.py:584`) | **no** — dropped in handler (`main.py:407-412`) |
| Persisted | **yes** — `jobs.idempotency_key` col + `state` `idem:` ns | **yes** — `state` `review_idem:` ns | **no** — nowhere (not Job, not jobs, not state) |
| In any audit event | **no** | **partial** — only `job.closed_no_action` (decline_to_act); not `human.reviewed` | **no** — not in any event; envelope ids are server-generated |
| Survives restart | **purged if job terminal** (startup purge) | **durable** (never purged) | n/a (never stored) |
| Dedup scope today | per-key, submit only | per-key, review only | **none** — has zero effect on dedup |

**Decision-relevant consequences (all live-verified on current code):**

1. **Submit idempotency is the wrong owner for source-event replay.** A submit
   `idempotency_key` is *purged the moment its job reaches a terminal state and the
   orchestrator restarts* (`idempotency_store._purge_terminal_keys`). A mobile client that
   re-sends the same source event after delivery + a hub restart will create a **duplicate
   job** (live: `CC-R6` produced a new `job_id`). Source-event identity needs durability
   *past* terminal state; submit idempotency deliberately does not provide that.

2. **Review idempotency is durable but scoped to the review decision, not the source
   event.** `decision_idempotency_key` survives restart (live: `CC-R8`) but only protects
   the *human review POST*, keyed on `(job_id, decision, result_id, response_hash,
   modified_result, decision_idempotency_key)`. It has no relationship to the originating
   client submit event and cannot deduplicate submits.

3. **There is currently no third identity, and no §6 replay guard.** `client_source_event_id`
   is accepted and then dropped before persistence and before audit. It is therefore *not*
   a durable identity today: it cannot dedup, cannot be queried, and cannot be reconstructed
   from the audit log. If the panel wants source-event replay protection, it must create the
   durable identity — nothing in the current code carries it.

4. **The contract intended preservation; only half shipped.** `docs/plans/phase-4a-backend-contract.md`
   AC#4 (schema accepts the fields) is implemented; AC#6 (the `job.submitted` event
   *preserves* those values) is **not** implemented and has **no test**. So today's behavior
   is "accept-then-drop," which is the gap the ADR is being asked to close.

---

## STEP 0 — Location, version, stack, routing

- **Path:** `C:\Users\ljeff\drnt-project`  **Branch:** `main`  **HEAD:** `2d035e3e5340b0fb7246b244a9fe50a70ea81756`
- **Tags present:** `v0.1.0`, `v0.2.0`, `v0.2.1`. `git describe --tags` = **`v0.2.1-34-g2d035e3`** (34 commits past `v0.2.1`, as expected "near v0.2.1"). Working tree clean.
- **How the stack starts:** `docker compose up` (compose file `docker-compose.yml` at repo root). Services: `drnt-orchestrator` (`127.0.0.1:8000`), `drnt-ollama` (`127.0.0.1:11434`), `drnt-audit-log-writer`, `drnt-egress-gateway`, `drnt-worker-proxy`, plus a build-only `worker` image. (`open-webui` also runs but is a standalone container, not part of this compose.)
- **Came up clean?** On arrival all six DRNT services were `healthy` (up ~2 days). `GET /health` → `orchestrator_status=running, audit_log_status=connected, ollama_status=available`, **`hub_suspended=true`**. The suspended hub blocks the pipeline worker (`job_manager.py:1053-1056` waits while `not is_processing_allowed()`); submissions are still accepted and sit at `submitted`. I issued `POST /resume` (runtime action) to process jobs.
  - **Caveat:** "clean" but **stale** — see the read-first caveat. I rebuilt the orchestrator from HEAD and recreated it; it came up healthy and carries the Phase 4A surface (`grep` of `/app/models.py` and `/app/job_manager.py` inside the new container confirmed `client_source_event_id`, `proposal_ready`, `_derive_proposal_hold_reason`).
- **Governed-path model routing — confirmed LOCAL before any live run:**
  - Orchestrator env: `OLLAMA_URL=http://ollama:11434` (the `drnt-ollama` container).
  - Local model inventory (`docker exec drnt-ollama ollama list`): **only `llama3.1:8b`**.
  - Classifier (`classifier.py`) sends both the classification call *and* the local generation to `llama3.1:8b`. A `quick_lookup` prompt is tagged `local_capable=true → routing=local` (`classifier.py:23,31-32,41-42`).
  - Capability `route.local` at WAL 0 has `dispatch_local: review_gate=pre_delivery` (config/capabilities.json), so a **local** job is held (`delivery_hold=True`) and reaches the **governed proposal path (`proposal_ready`) with no cloud call.** Live capability state: `route.local.effective_wal_level=0, status=active`.
  - **All 5 test jobs routed `ollama-llama3-local`. Zero cloud calls were made** (cost audit at end). I used the prompt `"What is the capital of France?"` for every submission and reused it across replays.

---

## STEP 1 — Three-ID ontology, code-level truth (file + line)

### 1) `idempotency_key` — submit retry guard
- **(a) Defined:** `orchestrator/models.py:69` `JobSubmitRequest.idempotency_key: Optional[str] = None`; internal record field `orchestrator/models.py:150` `Job.idempotency_key`.
- **(b) Accepted:** `orchestrator/main.py:411` — `submit_job` passes `idempotency_key=req.idempotency_key` into `job_manager.submit_job` (`job_manager.py:214`). If `None`, a UUIDv7 is auto-generated (`job_manager.py:223-224`). Dedup check `job_manager.py:229-235` via `IdempotencyStore.check_and_store` (`idempotency_store.py:213-231`).
- **(c) Persisted:** **YES, two places.** (i) `jobs` table column — `persistence.py:42` `idempotency_key TEXT UNIQUE`, written at `job_manager.py:178-179`. (ii) `state` table `idem:` namespace — `idempotency_store.py:41` (`_KEY_PREFIX="idem:"`), `:187-190` (`_db_write`). Live DB confirmed: `idem:wk1-0601-idem-CC-A → {job_id…}`.
- **(d) Audit:** **NO.** `event_job_submitted` (`events.py:142-157`) payload is `{raw_input, input_modality, device, request_category}` only. No occurrence of `idempotency_key` anywhere in `events.py`. Live `job.submitted` for JA confirms its absence.

### 2) `decision_idempotency_key` — human review retry guard
- **(a) Defined:** `orchestrator/models.py:87` `ReviewRequest.decision_idempotency_key: str` (**required**, not Optional).
- **(b) Accepted:** `orchestrator/main.py:584` passes it to `job_manager.review_job` (`job_manager.py:519+`). Replay is checked *before* stale-decision checks (`job_manager.py:541-543`), per plan amendment 4A.2.c rule 2.
- **(c) Persisted:** **YES** — `state` table `review_idem:` namespace (`idempotency_store.py:45` `_REVIEW_KEY_PREFIX="review_idem:"`; `store_review_outcome` `:296-318`; `_db_write_review` `:273-294`). Loaded on startup `:157-173`. **Not** touched by the terminal-key purge (purge only matches `idem:` — `idempotency_store.py:114-122`). Live DB confirmed: `review_idem:wk1-0601-decision-CC-1 → {payload_identity…}`.
- **(d) Audit:** **PARTIAL.** Reaches audit **only** via `event_job_closed_no_action` (`events.py:570-599`; key in payload at `:595`) — i.e. only on a `decline_to_act` decision. It is **not** in `event_human_reviewed` (`events.py:602-660`). Live-verified: JA's `human.reviewed` payload = `{job_id, decision, device, review_latency_ms, modification_summary, modified_result_id, modified_result_hash, derived_from_result_id}` — **no `decision_idempotency_key`**.

### 3) `client_source_event_id` (with `client_source`, `client_timestamp`) — mobile source-event identity candidate
- **(a) Defined:** `orchestrator/models.py:70-72` on `JobSubmitRequest` (`client_source: Optional[ClientSource]`, `client_source_event_id: Optional[str]`, `client_timestamp: Optional[str]`); `ClientSource` enum `:25-27`. **Not** present on the internal `Job` dataclass (`models.py:122-163`).
- **(b) Accepted at the boundary, then dropped:** declared on the request model with `model_config = ConfigDict(extra="forbid")` (`models.py:64`), so they are *accepted* (live: full payload → 202; unknown field → 422). **But** `submit_job` (`main.py:399-435`) forwards only `raw_input, input_modality, device, idempotency_key` to the job manager (`main.py:407-412`). The three client fields are read into the Pydantic model and then **discarded at the handler** — they never reach `job_manager.submit_job`, whose signature has no client parameters (`job_manager.py:209-215`). `main.py` contains **zero** references to `client_source`, `client_source_event_id`, or `replay`/`source_event_id`.
- **(c) Persisted:** **NO.** Not a `Job` field; not a `jobs` column; not in the `jobs.data` JSON; not in `state`. Live DB for JA and JB: serialized `data` JSON keys contain **no `client_*` keys** ("any client_* keys in data: NONE").
- **(d) Audit:** **NO.** `event_job_submitted` payload omits them (`events.py:151-156`). The envelope helper `build_event` (`events.py:26-50`) **always** server-generates `source_event_id=_uuid7()` (`:40`), `timestamp=_now_iso()` (`:41`), and defaults `source="orchestrator"` (`:35`) — there is **no parameter** to inject a client-supplied source/source-event-id/timestamp. Live `job.submitted` for JA: `source=orchestrator`, `source_event_id=019e844c-9037-…` (server, **not** my `wk1-0601-srcevent-CC1`), `timestamp=2026-06-01T17:47:50…` (server, **not** my `2026-06-01T12:00:00Z`).

**Contract vs. implementation:** `docs/plans/phase-4a-backend-contract.md` intended preservation — "Preserve mobile-origin lineage through submit" (`:98`), events.py should "use client-supplied source, source event ID, and timestamp when present" (`:106`), and **AC#6** (`:147`) requires a `job.submitted` event that "preserves those values." Implemented: **AC#4** (schema accepts fields; STATUS.md row 4A.3). Not implemented: **AC#6** — there is no preservation code and **no preservation test** (the only client-field tests, `tests/test_phase4a_backend_contract.py:110-151`, assert *request-model* acceptance only; STATUS.md has no preservation row).

**§6 source-event replay guard:** **does not exist in code.** No replay/source-event guard in `main.py` or `job_manager.py` for the mobile source event. (The `source_event_id` references in `job_manager.py` at lines 351/391/472/1175/1212 are the *override* and *permission-check* internal source events, and the audit-log-writer's `_seen_source_ids` dedup is on the *audit envelope's* server-generated `source_event_id` — neither is `client_source_event_id`.) This matches the task's statement that the guard is blocked behind the unclosed ADR.

---

## STEP 2 — §0 known-current-behavior claims

| Claim | Verdict | Evidence |
|---|---|---|
| Submit idempotency MAY still be purged after terminal jobs | **ACCEPTED-CLAIM** | Code: `idempotency_store._purge_terminal_keys` (`:98-129`) deletes `idem:` keys whose job is `delivered`/`failed` or missing; called on startup via `_load_from_db` (`:137`). Live: after orchestrator restart, startup log `"purged 1 stale idempotency key(s)"`; `idem:wk1-0601-idem-CC-A` (job JA delivered) → **PURGED**; replay `CC-R6` created a **new** job. |
| Review idempotency REMAINS durable across restart | **ACCEPTED-CLAIM** | Code: `review_idem:` records loaded on startup (`:157-173`) and **not** purged (purge matches only `idem:`). Live: after restart `review_idem:wk1-0601-decision-CC-1` → **PRESENT**; replay `CC-R8` returned the original stored outcome (200, delivered) even though JA was terminal. |
| `client_source_event_id` currently ACCEPTED at the API boundary | **ACCEPTED-CLAIM** | Code: `models.py:70-71` + `extra="forbid"` (`:64`). Live (current code): full payload incl. `client_source_event_id` → **202** (`CC_P1`); unknown field → **422** (`CC_neg`), proving the field is a *known* accepted field, not silently ignored. |
| `client_source_event_id` is currently DROPPED before persistence/audit | **ACCEPTED-CLAIM** (it *is* dropped) | Code: dropped at `main.py:407-412`; absent from `Job`/`jobs`/`state`/all events. Live: JA `job.submitted` carries no client fields and server-generated envelope ids; JA/JB `jobs.data` JSON has no `client_*` keys. |

All four are mostly code-visible; live runs corroborate.

---

## STEP 3 — §6A baseline failure proof (live, observational, current code)

Canonical request (`CC_P1_request.json`), reused across replays:
```json
{"raw_input":"What is the capital of France?","input_modality":"text","device":"phone",
 "client_source":"phone_app","client_source_event_id":"wk1-0601-srcevent-CC1",
 "client_timestamp":"2026-06-01T12:00:00Z","idempotency_key":"wk1-0601-idem-CC-A"}
```

- **Do the fields enter the submit/job path?** `client_source` is enum-validated and `client_source_event_id`/`client_timestamp` are accepted into the request model (HTTP 202, `CC_P1_submit_JA.json`, `job_id` **JA**=`019e844c-901d-7613-a6ec-e53085d7048d`), but **none of the three enter the job path** — they are dropped at the handler. `idempotency_key` is the only client-supplied identity that flows downstream.
- **Are they persisted?** **No.** `jobs.data` JSON for JA (and JB) contains no `client_*` keys (`db_jobs_J1_J2.txt` for the stale run; current-code DB check returned the same key set). No `client_*` column exists.
- **Do they appear in the `job.submitted` audit event?** **No** (`CC_P1_audit_JA.jsonl`): `source=orchestrator`, server `source_event_id`/`timestamp`, payload `[device, input_modality, raw_input, request_category]`.
- **Replay the SAME `client_source` + `client_source_event_id`:**
  - With the **same** `idempotency_key` → **dedup** to JA (`CC_R1_replay_samekey.json`).
  - With a **different** `idempotency_key` (same `client_source_event_id`) → **new job JB**=`019e844d-38b7-7d41-9774-c962f6eaba95` (`CC_R2_replay_diffkey.json`).
- **Duplicate work, dropped lineage, or both? → BOTH.** One source event (`wk1-0601-srcevent-CC1`) produced two independent jobs (JA, JB), each of which independently classified and generated on `llama3.1:8b` (and both reached `proposal_ready` — two separate governed proposals). The originating client event id is recorded **nowhere**, so the two duplicates cannot even be correlated back to the one source event.

Raw artifacts: `CC_neg_unknown_field.json`, `CC_P1_request.json`, `CC_P1_submit_JA.json`,
`CC_P1_get_JA.json`, `CC_P1_audit_JA.jsonl`, `CC_R1_replay_samekey.json`,
`CC_R2_request.json`, `CC_R2_replay_diffkey.json`. (Stale-image equivalents: `S1_*`, `R1_*`, `R2_*`, `db_*`.)

---

## STEP 4 — Replay timing, current (pre-fix) behavior only

The same minimal request was reused (no new content generated). There is **no source-event
guard**, so replay outcome is governed entirely by `idempotency_key` (submit) state:

| Stage | What was replayed | Observed current behavior | Artifact |
|---|---|---|---|
| **before classification** | same `idempotency_key`, job still `submitted` (hub suspended) | **dedup** → returns existing job (no duplicate) | `CC_R1_replay_samekey.json` |
| **after `proposal_ready`** | same `idempotency_key`, job held in `proposal_ready` | **dedup** → returns existing job | `CC_R3_replay_after_proposalready.json` |
| **after approve/edit delivery (no restart)** | same `idempotency_key`, job `delivered` (terminal) | **dedup** → returns existing job; in-memory key is *not* purged on terminal transition | `CC_R5_submit_after_delivery.json` |
| **after a stack restart** | same `idempotency_key`, job was `delivered` (terminal) | **NEW job** `019e844f-ff11-…` (**duplicate work**) — key purged at startup | `CC_R6_submit_after_restart.json` |
| (restart, control) | same `idempotency_key` for a **non-terminal** job (JB, `proposal_ready`) | **dedup** → returns JB (key survived; job reloaded) | `CC_R7_submit_KB_after_restart.json` |
| (restart, review) | same `decision_idempotency_key` for delivered JA | **dedup** → original stored review outcome (review idem durable) | `CC_R8_review_after_restart.json` |

**Plain-language summary of *today's* behavior:** Re-sending the same mobile source event is
deduplicated only while the submit `idempotency_key` is still live in the store — i.e. before
terminal, or after terminal until the next orchestrator restart. Once the job is terminal
**and** the orchestrator restarts, the submit key is purged and the *same source event*
creates a brand-new duplicate job. The review key behaves oppositely (durable across restart),
but it only guards the review POST, not the submit. The mobile `client_source_event_id`
plays no role in any of this at any stage.

---

## STEP 5 — Audit-lineage gap (§7), current state

Target chain: `source client → client event ID → client timestamp → server job_id →
proposal_id → review decision → final state`. Reconstructed from JA's full audit trail
(`CC_JA_audit_full.jsonl`, ordered):

```
job.submitted              src=orchestrator                     (no client identity)
job.classified             src=orchestrator   routing=local
wal.permission_check       src=orchestrator   delivery_hold=True dispatch_local
worker.prepared / job.dispatched / worker.execution_started / worker.execution_completed
model.response             result_id=019e844d-9254-…
job.response_received      result_id=019e844d-9254-…
job.proposal_ready         proposal_id=019e844d-93e5-…  result_id=019e844d-9254-…  hold_reason=pre_delivery
worker.teardown
human.reviewed             src=human          decision=approve
job.delivered
```

| Link | Present today? | Evidence |
|---|---|---|
| source client (`client_source`) | **MISSING** | absent from all 13 JA events; `source` is always `orchestrator`/`human` |
| client event ID (`client_source_event_id`) | **MISSING** | 0 of 13 events; envelope `source_event_id` is server-generated |
| client timestamp (`client_timestamp`) | **MISSING** | 0 of 13 events; envelope `timestamp` is server time |
| server `job_id` | **present** | on every event |
| `proposal_id` | **present** | `job.proposal_ready` |
| review decision | **present** | `human.reviewed.decision=approve` (note: its `decision_idempotency_key` is **not** in `human.reviewed`; only `job.closed_no_action` carries it) |
| final state | **present** | `job.delivered` |

**Conclusion:** The server-side chain (`job_id → result_id → proposal_id → review decision →
final state`) is **fully reconstructable** from the audit log today. The **client-origin
links are entirely missing**: there is no way, from the audit trail, to connect a server
`job_id` back to the mobile source client, its event id, or its timestamp. The lineage
breaks at exactly the boundary the ADR concerns.

---

## (1) Could NOT determine

1. **Behavior of the *intended/future* §6 source-event replay guard** — it does not exist in
   code, so there is nothing to observe. By design I did not implement or simulate it.
2. **`edit`/`decline_to_act` review-decision audit specifics live** — I exercised only
   `approve` to keep model cost minimal. I confirmed by code (`events.py:570-599`) and by the
   absence of `decision_idempotency_key` in `human.reviewed` that the review key reaches audit
   only via `job.closed_no_action` (decline_to_act), but I did **not** live-fire a
   `decline_to_act` to capture that event.
3. **Exact pre-classification timing window** — I observed the "before classification" replay
   deterministically by holding the job at `submitted` via the suspended hub, not by racing a
   live classifier. The dedup outcome is stage-independent (key stored synchronously at submit),
   so this does not affect the conclusion, but I did not measure a true sub-second race.
4. **Whether other services (egress-gateway, worker-proxy, audit-log-writer) are also stale**
   relative to HEAD — I only rebuilt the orchestrator (the sole owner of the source-event
   surface). I confirmed the on-arrival audit-log-writer has no event-type allowlist
   (`event_validator.py:64-66`), so it accepts the new event types, but I did not audit the
   other images' build dates.
5. **Production/default operational intent of `hub_suspended=true`** — I found the hub
   suspended, resumed it to run the pipeline, and (see note) restored it. Whether suspended is
   the operator's intended steady state is not determinable from code.

## (2) Model / API call count (cost audit)

- **Total live model calls: 10, all to the LOCAL `llama3.1:8b` in the `drnt-ollama` container. Cloud calls: 0.**
- Breakdown (from `model.response` + `job.classified` audit events for my 5 test jobs; all `route_id=ollama-llama3-local`): **5 classification calls + 5 generation calls** across 5 jobs (J1, J2 on the stale image; JA, JB, JC on current code). Each job = 1 classify + 1 generate.
- No retries or loops were issued. All submissions used the single prompt `"What is the capital of France?"`, reused across replays so replays exercised idempotency/source-event logic rather than fresh generation.
- Live HTTP submissions issued (non-generating where noted): 3 stale-image (`S1`, `R1` dedup, `R2`) + ~12 current-code (`CC_neg` 422, `CC_P1`, `CC_R1`/`R3`/`R5` dedup, `CC_R2` new, `CC_R6` new, `CC_R7` dedup, `CC_R8` review replay, 1 approve, 1 review replay) ≈ **15 submissions / a dozen-ish**, well under the "hundreds" ceiling.

---

### Runtime-state notes (no repo code changed)
- Rebuilt + recreated the `drnt-orchestrator` container from repo HEAD (image refresh only).
- Issued `POST /resume` (and again after the restart) to allow pipeline processing; issued one
  `docker restart drnt-orchestrator` to exercise the startup purge.
- Left test jobs/keys (JA delivered; JB, JC in `proposal_ready`) in the disposable runtime.
- All raw artifacts are saved alongside this report in `DRNT_Week1_Phase4A2_Evidence/artifacts/`.
