# Audit-Log Integrity: Segmented Chains, Verification & Recovery (Patch 0)

The orchestrator runs an `audit_integrity` startup check
(`orchestrator/startup_validator.py:check_audit_integrity`). It is a CRITICAL,
fail-closed gate: if the audit log's hash chain is broken it refuses to start.
There is no bypass env var, and that is intentional.

## The audit log is segmented, not one global chain

This is the key mental model. The audit log is a sequence of independently
**genesis-anchored segments**, not a single unbroken chain from the first record
ever written.

The audit-log-writer's `recover_chain_state()` reads only **yesterday + today**
to rebuild `prev_hash` on startup. When it opens a new daily file after a gap of
more than a day, it cannot see the prior file, so it re-anchors `prev_hash` to
`SHA-256("DRNT-GENESIS")` and begins a **new segment**. A real accumulated
volume therefore contains several segments, each starting with a genesis record
at the first line of a daily file. For example, the dev volume as of 2026-06-04
held **85,249 records across 5 segments** (re-anchors at `04-03`, `04-10`,
`04-18`, `05-29`, `06-04`), and it is **completely valid**.

Integrity verification is **segment-aware**:

- `prev_hash == GENESIS` is valid **only at a segment start** — the first record
  of a file. Genesis anywhere else (mid-file) is a break. This is *not* "accept
  genesis anywhere".
- Within a segment the chain must be unbroken (each `prev_hash` equals the hash
  of the previous line).
- A file whose first record continues from the previous file's last hash (no
  gap, no re-anchor) is a normal continuation — still one segment.

This applies in two places, which use the same rule:
`orchestrator/audit_integrity.py` (the verifier/CLI) and
`orchestrator/startup_validator.py:_verify_tail_chain` (the startup gate, over
its bounded last-N window).

> Historical note: before this patch the startup gate verified the last ~100
> records with no segment awareness. When a new daily file re-anchored at
> genesis within that window (e.g. the `06-01 → 06-04` boundary), the gate
> misread the legitimate re-anchor as "hash chain broken" and the orchestrator
> crash-looped — even though the data was intact. That was a **false positive in
> the check**, not corruption. Patch 0 makes the check segment-aware. No audit
> data was modified.

## What a genuine failure looks like

When the gate fails for a real reason, the orchestrator logs one precise,
actionable line (visible via `docker compose logs orchestrator`) and exits — it
does not hang:

```
Startup validation CRITICAL failure [audit_integrity]: audit_integrity failed:
broken hash-chain tail detected. Valid records: N. First invalid record: M.
Repairable: yes. No audit data was deleted. Run: python -m audit_integrity
repair-tail --path /var/drnt/audit --out /var/drnt/audit/repair-report.json ...
```

Classification:

- **`broken_tail`** (repairable): a non-empty valid prefix followed by a corrupt
  suffix with no coherent continuation — a torn / partial write at the live tail.
- **`interior_corruption`** (NOT repairable): a valid record continues *after*
  the break (within a segment), or the first record of the log is itself invalid.
  Fail closed; investigate manually.

The e2e preflight (`tests/conftest.py`) detects this same `audit_integrity
failed:` signature in the orchestrator logs and fails fast with `E2E blocked by
audit_integrity startup failure` instead of waiting out the health timeout.

## Verifying a volume (read-only, no mutation)

The `orchestrator` service mounts the audit volume **read-only**, so it can only
verify. Verify from any container carrying the orchestrator code; confirm the
real volume / image names first (`docker volume ls`, `docker compose images
orchestrator`):

```bash
docker run --rm \
  -v drnt-project_audit-logs:/var/drnt/audit:ro \
  drnt-project-orchestrator \
  python -m audit_integrity verify --path /var/drnt/audit
```

A healthy multi-segment volume reports `"status": "valid"` with a
`"... across K segment(s)."` message. (You can also mount just the source file
into a stock `python:3.12-slim` image if you have not rebuilt the orchestrator.)

## Recovery runbook — only for a genuine `broken_tail`

Use this **only** when verification reports `broken_tail` (a torn write at the
live tail), not for segment boundaries and not for `interior_corruption`. Repair
must run from a container with a **read-write** mount of the audit volume.

### Step 1 — Snapshot the volume (back up first)

```bash
docker run --rm \
  -v drnt-project_audit-logs:/audit:ro \
  -v "$PWD/audit-backup:/backup" \
  alpine sh -c 'cd /audit && tar -czf /backup/audit-logs-before-repair.tgz .'
```

### Step 2 — Report-only verification

```bash
docker run --rm -v drnt-project_audit-logs:/var/drnt/audit:ro \
  drnt-project-orchestrator \
  python -m audit_integrity verify --path /var/drnt/audit
```

Expect `"status": "broken_tail"`, `"repairable": true`. **If it reports
`interior_corruption`, stop** (see below). If it reports `valid`, there is
nothing to repair.

### Step 3 — Tail repair (explicit, RW mount)

```bash
docker run --rm -v drnt-project_audit-logs:/var/drnt/audit \
  drnt-project-orchestrator \
  python -m audit_integrity repair-tail \
    --path /var/drnt/audit --out /var/drnt/audit/repair-report.json
```

This copies the corrupt suffix **verbatim** into
`/var/drnt/audit/quarantine/audit-tail-<ts>.jsonl` (bytes preserved, digest
recorded), seals the valid prefix by truncating the break file at the first
corrupt byte, writes a manifest (`repair-report.json`, including
`last_valid_hash`), and re-verifies. The corrupt bytes are never deleted. No new
physical segment file is created — the writer resumes from `last_valid_hash` via
its normal daily-file recovery.

### Step 4 — Start the orchestrator (integrity still enforced)

```bash
docker compose up -d orchestrator    # audit_integrity stays enabled; no bypass
```

### Step 5 — Run the official boundary probe

```bash
curl -s -X POST http://127.0.0.1:8000/admin/e2e/egress-probe | jq
```

(The hub must be active first: `POST /confirm_authority` then `POST /resume`.)

## Interior corruption — do not auto-repair

If verification reports `interior_corruption`, the tool refuses to repair and
nothing is mutated: a record mid-segment is inconsistent while later records
continue — not a torn tail. Restore from a trusted snapshot, inspect around the
`first_invalid_record` index, and do **not** truncate or hand-edit the live log
to make startup pass; that destroys the tamper-evidence the chain provides.

## Known limitation

Forward hash chaining detects tampering of a record only via its successor's
`prev_hash`. Editing the *second-to-last* record (which then has exactly one
successor) is indistinguishable from a torn final write. Repair therefore always
preserves the quarantined bytes and sets `operator_required: true`. Segment
boundaries are anchored at file starts; an attacker with write access to the
volume could in principle truncate-and-re-anchor a new file — addressing that
requires out-of-band anchoring (segment-boundary sealing), which is out of scope
for this patch.
