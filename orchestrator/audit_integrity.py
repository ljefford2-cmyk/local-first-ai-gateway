"""Audit-log integrity verification and tail-repair (Patch 0).

Self-contained (stdlib only) so it can run as ``python -m audit_integrity``
inside any DRNT container that carries the orchestrator code, or be imported by
``startup_validator`` and the e2e preflight.

Three responsibilities:

  * ``verify_path(path)``  — classify a log dir/file set as
    ``valid`` / ``empty`` / ``broken_tail`` / ``interior_corruption`` WITHOUT
    mutating anything. Report-only.
  * ``repair_tail(path, out)`` — for a *repairable* broken tail: quarantine the
    corrupt suffix (bytes preserved exactly), seal the valid prefix, and write a
    repair manifest. Refuses interior corruption and refuses to delete history.
  * CLI: ``verify`` and ``repair-tail`` subcommands.

Read vs write split (deliberate)
--------------------------------
The orchestrator mounts the audit volume **read-only**, so at startup it can
only VERIFY (``startup_validator.check_audit_integrity``) and fail fast with an
actionable message. REPAIR is an explicit operator action run from a container
or host with a read-write mount of the audit volume — see
``docs/AUDIT-RECOVERY.md``. Repair is never performed during startup.

Segmented chain (not one global unbroken chain)
-----------------------------------------------
The audit log is a sequence of independently genesis-anchored **segments**, not
one continuous chain. The audit-log-writer's ``recover_chain_state`` reads only
yesterday + today, so when it opens a new daily file after a gap it cannot see
the prior file and re-anchors ``prev_hash`` to genesis, starting a new segment.
Verification therefore treats ``prev_hash == GENESIS`` as valid ONLY at a
**segment start** — the first record of a file (see :func:`_segment_starts`).
Within a segment the chain must be unbroken, and genesis appearing anywhere but
a file's first record is a break. This is *not* "accept genesis anywhere".

Tail vs interior corruption (the load-bearing distinction)
----------------------------------------------------------
We verify the segmented chain and find the first invalid record ``F``.
Records ``[0, F)`` form a valid (multi-segment) chain. The suffix ``[F, end]``
is corrupt.

  * **broken_tail** (repairable): there is a non-empty valid prefix AND the
    corrupt suffix contains no internally coherent continuation — it looks like
    a torn / partial write or trailing garbage. Quarantining it loses no valid
    audit data.
  * **interior_corruption** (NOT repairable): a valid record continues *after*
    the first break (the chain re-establishes itself within the suffix), or the
    very first record is already invalid (no trusted prefix). That is not a torn
    tail; a record was likely modified or removed mid-stream. Fail closed and
    require manual investigation.

Known limitation: forward hash chaining can only detect tampering of a record
via its successor's ``prev_hash``. Editing the *second-to-last* record (which
then has exactly one successor) is indistinguishable from a torn final write.
Repair therefore always preserves the quarantined bytes and sets
``operator_required: true`` so the suffix can be inspected after the fact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENESIS_STRING = "DRNT-GENESIS"
GENESIS_HASH = hashlib.sha256(GENESIS_STRING.encode("utf-8")).hexdigest()

LOG_GLOB = "drnt-audit-*.jsonl"

# Stable marker that prefixes every audit-integrity startup failure message.
# startup_validator emits it; the e2e preflight greps orchestrator logs for it.
AUDIT_FAILURE_MARKER = "audit_integrity failed:"

# Status values
STATUS_VALID = "valid"
STATUS_EMPTY = "empty"
STATUS_BROKEN_TAIL = "broken_tail"
STATUS_INTERIOR = "interior_corruption"


# ---------------------------------------------------------------------------
# Hashing (byte-exact with audit-log-writer: sha256 of the line's utf-8 bytes,
# excluding the trailing newline)
# ---------------------------------------------------------------------------


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def compute_hash(json_line: str) -> str:
    """SHA-256 hex digest of a JSON line (utf-8, no trailing newline)."""
    return _hash_bytes(json_line.encode("utf-8"))


# ---------------------------------------------------------------------------
# Record model
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """One non-empty physical line of an audit log, with byte coordinates.

    ``byte_start`` / ``byte_end`` are offsets within ``file``; ``byte_end``
    includes the trailing newline if one was present, so truncating ``file`` at
    the ``byte_start`` of the first corrupt record removes that record's bytes
    exactly while preserving every prior byte verbatim.
    """

    index: int  # global index across all files (only non-empty lines counted)
    file: Path
    byte_start: int
    byte_end: int
    raw: bytes  # line bytes, excluding the newline
    text: str
    parsed: dict | None  # parsed JSON object, or None if malformed / not an object


def _read_file_records(path: Path, start_index: int) -> list[Record]:
    """Read one file into Records, tracking exact byte offsets."""
    data = path.read_bytes()
    records: list[Record] = []
    idx = start_index
    n = len(data)
    start = 0
    while start < n:
        nl = data.find(b"\n", start)
        if nl == -1:
            raw = data[start:n]  # partial final line (no newline)
            line_end = n
        else:
            raw = data[start:nl]  # exclusive of newline
            line_end = nl + 1  # consumed span includes the newline
        if raw.strip():
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed, dict):
                    parsed = None
            except Exception:
                parsed = None
            records.append(
                Record(
                    index=idx,
                    file=path,
                    byte_start=start,
                    byte_end=line_end,
                    raw=raw,
                    text=raw.decode("utf-8", errors="replace"),
                    parsed=parsed,
                )
            )
            idx += 1
        start = line_end
        if nl == -1:
            break
    return records


def collect_log_files(path: str | Path) -> list[Path]:
    """Resolve a directory or single file to an ordered list of log files."""
    p = Path(path)
    if p.is_dir():
        return sorted(p.glob(LOG_GLOB))
    if p.is_file():
        return [p]
    return []


def read_records(files: list[Path]) -> list[Record]:
    """Read all log files (in given order) into a flat, globally-indexed list."""
    records: list[Record] = []
    for f in files:
        records.extend(_read_file_records(f, start_index=len(records)))
    return records


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@dataclass
class AuditReport:
    status: str
    records_total: int
    records_valid: int
    first_invalid_record: int | None  # global index, or None if no break
    last_valid_record: int | None  # global index, or None if no valid prefix
    last_valid_hash: str  # hash to anchor a repaired/continued chain
    repairable: bool
    corrupt_suffix_sha256: str | None
    files: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "records_total": self.records_total,
            "records_valid": self.records_valid,
            "first_invalid_record": self.first_invalid_record,
            "last_valid_record": self.last_valid_record,
            "last_valid_hash": self.last_valid_hash,
            "repairable": self.repairable,
            "corrupt_suffix_sha256": self.corrupt_suffix_sha256,
            "files": self.files,
            "message": self.message,
        }


def _chain_hash(rec: Record) -> str:
    """Hash of a record's line as the writer hashed it.

    The audit-log-writer hashes the JSON text with no trailing newline. We read
    files as raw bytes (for byte-exact repair), so a log that was ever rewritten
    through a CRLF tool would carry a trailing ``\\r``; exclude it here so chain
    verification matches the writer. ``rec.raw`` itself stays byte-exact.
    """
    raw = rec.raw[:-1] if rec.raw.endswith(b"\r") else rec.raw
    return _hash_bytes(raw)


def _segment_starts(records: list[Record]) -> list[bool]:
    """Mark each record that begins a new audit segment (first record of a file).

    The audit log is segmented, not one global chain: the audit-log-writer
    re-anchors at genesis whenever it opens a new daily file after a gap
    (``recover_chain_state`` reads only yesterday + today, so it cannot see an
    older file). A ``prev_hash == GENESIS`` is therefore legitimate ONLY at the
    first record of a file. Genesis anywhere else is treated as a break — we do
    not "accept genesis anywhere".
    """
    flags: list[bool] = []
    prev_file = None
    for rec in records:
        flags.append(rec.file != prev_file)
        prev_file = rec.file
    return flags


def _first_break(records: list[Record], seg_starts: list[bool]) -> int:
    """First record that is neither a valid continuation nor a legitimate
    segment start, or -1 if the (segmented) chain is intact.

    Each segment is verified independently from its genesis re-anchor; within a
    segment the chain must be unbroken.
    """
    prev = GENESIS_HASH
    for i, rec in enumerate(records):
        if rec.parsed is None:
            return i
        ph = rec.parsed.get("prev_hash")
        if ph == prev:
            prev = _chain_hash(rec)  # normal link (within, or continuous across, files)
            continue
        if seg_starts[i] and ph == GENESIS_HASH:
            prev = _chain_hash(rec)  # legitimate new-segment re-anchor at a file boundary
            continue
        return i
    return -1


def _has_coherent_continuation(
    records: list[Record], seg_starts: list[bool], first_invalid: int
) -> bool:
    """True if valid audit data continues after the first break — a coherent
    link or a legitimate new segment start. Signature of interior corruption
    (edit/deletion mid-stream) rather than a torn tail.
    """
    for i in range(first_invalid + 1, len(records)):
        b = records[i]
        if b.parsed is None:
            continue
        ph = b.parsed.get("prev_hash")
        if seg_starts[i] and ph == GENESIS_HASH:
            return True
        a = records[i - 1]
        if a.parsed is not None and ph == _chain_hash(a):
            return True
    return False


def _suffix_sha256(records: list[Record], first_invalid: int) -> str:
    """SHA-256 over the concatenated raw bytes (incl. newlines) of the suffix."""
    h = hashlib.sha256()
    for rec in records[first_invalid:]:
        h.update(rec.raw)
        if rec.byte_end > rec.byte_start + len(rec.raw):
            h.update(b"\n")  # the line had a trailing newline
    return h.hexdigest()


def classify(records: list[Record], files: list[Path] | None = None) -> AuditReport:
    """Classify a list of records into an :class:`AuditReport` (no mutation)."""
    file_names = [str(f) for f in (files or [])]
    total = len(records)

    if total == 0:
        return AuditReport(
            status=STATUS_EMPTY,
            records_total=0,
            records_valid=0,
            first_invalid_record=None,
            last_valid_record=None,
            last_valid_hash=GENESIS_HASH,
            repairable=False,
            corrupt_suffix_sha256=None,
            files=file_names,
            message="No audit records present; a new chain initializes from genesis.",
        )

    seg_starts = _segment_starts(records)
    first_invalid = _first_break(records, seg_starts)

    if first_invalid == -1:
        last = records[-1]
        # In a valid chain every genesis prev_hash sits at a segment start, so
        # counting them gives the number of independent segments.
        segments = sum(
            1
            for r in records
            if r.parsed is not None and r.parsed.get("prev_hash") == GENESIS_HASH
        )
        return AuditReport(
            status=STATUS_VALID,
            records_total=total,
            records_valid=total,
            first_invalid_record=None,
            last_valid_record=last.index,
            last_valid_hash=_chain_hash(last),
            repairable=False,
            corrupt_suffix_sha256=None,
            files=file_names,
            message=(
                f"Chain valid, {total} records verified across {segments} "
                f"segment(s)."
            ),
        )

    records_valid = first_invalid
    last_valid_index = first_invalid - 1
    last_valid_hash = (
        _chain_hash(records[last_valid_index])
        if last_valid_index >= 0
        else GENESIS_HASH
    )
    suffix_digest = _suffix_sha256(records, first_invalid)

    # No trusted prefix, or a coherent chain continues past the break -> interior.
    interior = records_valid == 0 or _has_coherent_continuation(
        records, seg_starts, first_invalid
    )

    if interior:
        if records_valid == 0:
            reason = "first record does not chain from genesis (no trusted prefix)"
        else:
            reason = "a valid record continues after the first break"
        return AuditReport(
            status=STATUS_INTERIOR,
            records_total=total,
            records_valid=records_valid,
            first_invalid_record=first_invalid,
            last_valid_record=last_valid_index if last_valid_index >= 0 else None,
            last_valid_hash=last_valid_hash,
            repairable=False,
            corrupt_suffix_sha256=suffix_digest,
            files=file_names,
            message=f"Interior corruption: {reason}. Manual investigation required.",
        )

    return AuditReport(
        status=STATUS_BROKEN_TAIL,
        records_total=total,
        records_valid=records_valid,
        first_invalid_record=first_invalid,
        last_valid_record=last_valid_index,
        last_valid_hash=last_valid_hash,
        repairable=True,
        corrupt_suffix_sha256=suffix_digest,
        files=file_names,
        message=(
            f"Broken hash-chain tail: {records_valid} valid records, "
            f"corrupt suffix from record {first_invalid}."
        ),
    )


def verify_path(path: str | Path) -> AuditReport:
    """Read every log file under ``path`` and classify the chain. No mutation."""
    files = collect_log_files(path)
    records = read_records(files)
    return classify(records, files)


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


class RepairRefused(Exception):
    """Raised when a repair is requested but not safe to perform."""


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def repair_tail(path: str | Path, out: str | Path | None = None) -> dict:
    """Repair a repairable broken tail. Refuses anything else.

    Steps (in order, quarantine before any destructive action):
      1. Re-classify; require ``status == broken_tail`` and ``repairable``.
      2. Copy the corrupt suffix bytes verbatim into ``quarantine/`` and digest.
      3. Seal the valid prefix: truncate the break file at the first corrupt
         byte; move any newer (fully-corrupt) files into quarantine.
      4. Write a repair manifest and re-verify the sealed prefix.

    Returns the manifest dict. Raises :class:`RepairRefused` if not repairable;
    nothing is mutated in that case.
    """
    root = Path(path)
    files = collect_log_files(root)
    records = read_records(files)
    report = classify(records, files)

    if report.status == STATUS_VALID:
        raise RepairRefused("Chain is valid; nothing to repair.")
    if report.status == STATUS_EMPTY:
        raise RepairRefused("Audit log is empty; nothing to repair.")
    if not report.repairable or report.status != STATUS_BROKEN_TAIL:
        raise RepairRefused(
            f"Refusing to repair: status={report.status} (repairable=False). "
            "Interior corruption requires manual investigation; no data was touched."
        )

    first_invalid = report.first_invalid_record
    assert first_invalid is not None
    break_record = records[first_invalid]
    break_file = break_record.file

    # Newer files (after the break file) are entirely part of the corrupt suffix.
    newer_files = [f for f in files if f.name > break_file.name]

    quarantine_dir = root / "quarantine" if root.is_dir() else break_file.parent / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()
    quarantine_path = quarantine_dir / f"audit-tail-{stamp}.jsonl"

    # 1. Quarantine corrupt bytes verbatim: break file's tail, then newer files.
    break_file_bytes = break_file.read_bytes()
    corrupt_tail = break_file_bytes[break_record.byte_start:]
    digest = hashlib.sha256()
    with open(quarantine_path, "wb") as q:
        q.write(corrupt_tail)
        digest.update(corrupt_tail)
        for nf in newer_files:
            nf_bytes = nf.read_bytes()
            q.write(nf_bytes)
            digest.update(nf_bytes)
        q.flush()
        os.fsync(q.fileno())
    corrupt_sha256 = digest.hexdigest()

    # 2. Seal the valid prefix: truncate the break file at the first corrupt byte.
    with open(break_file, "r+b") as bf:
        bf.truncate(break_record.byte_start)
        bf.flush()
        os.fsync(bf.fileno())

    # 3. Remove the newer (fully quarantined) files.
    removed = []
    for nf in newer_files:
        nf.unlink()
        removed.append(nf.name)

    # 4. Manifest.
    manifest = {
        "repair_id": stamp,
        "repair_type": "tail_quarantine",
        "records_total": report.records_total,
        "records_valid": report.records_valid,
        "last_valid_record_index": report.last_valid_record,
        "last_valid_hash": report.last_valid_hash,
        "corrupt_from_record_index": first_invalid,
        "corrupt_suffix_path": str(quarantine_path),
        "corrupt_suffix_sha256": corrupt_sha256,
        "sealed_file": break_file.name,
        "removed_files": removed,
        # The audit-log-writer resumes the chain from last_valid_hash via its
        # daily-file recovery; no new physical segment file is created here
        # because the writer discovers files by date glob (drnt-audit-<date>).
        "new_segment_anchor_hash": report.last_valid_hash,
        "operator_required": True,
    }

    # 5. Re-verify the sealed prefix.
    post = verify_path(root)
    manifest["post_repair_status"] = post.status
    manifest["post_repair_records"] = post.records_total

    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    return manifest


# ---------------------------------------------------------------------------
# Startup-failure message + e2e preflight detection
# ---------------------------------------------------------------------------


def _repair_command(path: str) -> str:
    return f"python -m audit_integrity repair-tail --path {path} --out {path}/repair-report.json"


def format_startup_failure(report: AuditReport, audit_path: str = "/var/drnt/audit") -> str:
    """Build the actionable, fail-fast message for a failed audit-integrity check.

    Always begins with :data:`AUDIT_FAILURE_MARKER` so the e2e preflight can
    detect it in orchestrator logs.
    """
    if report.status == STATUS_BROKEN_TAIL:
        return (
            f"{AUDIT_FAILURE_MARKER} broken hash-chain tail detected. "
            f"Valid records: {report.records_valid}. "
            f"First invalid record: {report.first_invalid_record}. "
            f"Repairable: yes. No audit data was deleted. "
            f"Run: {_repair_command(audit_path)} "
            f"(needs a read-write mount of the audit volume; see docs/AUDIT-RECOVERY.md)"
        )
    if report.status == STATUS_INTERIOR:
        return (
            f"{AUDIT_FAILURE_MARKER} interior corruption detected. "
            f"Valid records: {report.records_valid}. "
            f"First invalid record: {report.first_invalid_record}. "
            f"Repairable: no. Manual investigation required. "
            f"See docs/AUDIT-RECOVERY.md"
        )
    # Fallback (e.g. unreadable) — still actionable and still fail-closed.
    return (
        f"{AUDIT_FAILURE_MARKER} {report.message} "
        f"Run: python -m audit_integrity verify --path {audit_path}"
    )


def is_audit_startup_failure(log_text: str) -> bool:
    """True if orchestrator log output shows an audit-integrity startup failure."""
    return bool(log_text) and AUDIT_FAILURE_MARKER in log_text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_report(report: AuditReport) -> None:
    print(json.dumps(report.to_dict(), indent=2))


def _cmd_verify(args: argparse.Namespace) -> int:
    report = verify_path(args.path)
    _print_report(report)
    if args.report_json:
        Path(args.report_json).write_text(
            json.dumps(report.to_dict(), indent=2) + "\n", encoding="utf-8"
        )
    return 0 if report.status in (STATUS_VALID, STATUS_EMPTY) else 1


def _cmd_repair_tail(args: argparse.Namespace) -> int:
    try:
        manifest = repair_tail(args.path, out=args.out)
    except RepairRefused as exc:
        print(f"repair-tail refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(manifest, indent=2))
    ok = manifest.get("post_repair_status") in (STATUS_VALID, STATUS_EMPTY)
    if not ok:
        print(
            "WARNING: post-repair verification did not return a healthy chain.",
            file=sys.stderr,
        )
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="audit_integrity",
        description="DRNT audit-log integrity verification and tail repair.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    v = sub.add_parser("verify", help="Report-only chain verification.")
    v.add_argument("--path", required=True, help="Audit log directory or file.")
    v.add_argument("--report-json", help="Write the structured report to this file.")
    v.set_defaults(func=_cmd_verify)

    r = sub.add_parser(
        "repair-tail",
        help="Quarantine a broken tail and seal the valid prefix (RW mount).",
    )
    r.add_argument("--path", required=True, help="Audit log directory or file.")
    r.add_argument("--out", help="Write the repair manifest to this file.")
    r.set_defaults(func=_cmd_repair_tail)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
