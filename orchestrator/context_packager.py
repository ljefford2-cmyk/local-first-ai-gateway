"""Context Packager — inline deny-list filter for cloud-bound payloads.

Sits between WAL permission check and egress gateway dispatch.
Scans raw user input for sensitive content using regex patterns,
strips or generalizes matches, and assembles the outbound payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import uuid_utils

from audit_client import AuditLogClient

logger = logging.getLogger(__name__)


def _uuid7() -> str:
    return str(uuid_utils.uuid7())


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# Action precedence: strip > generalize > pass
ACTION_PRECEDENCE = {"strip": 0, "generalize": 1, "pass": 2}


@dataclass
class SensitivityPattern:
    """A compiled regex pattern with its class and action."""

    regex: re.Pattern
    sensitivity_class: str
    action: str
    hardcoded: bool
    pattern_source: str  # Original pattern string for audit trail


@dataclass
class DetectedSpan:
    """A span of text that matched a sensitivity pattern."""

    start: int
    end: int
    sensitivity_class: str
    action: str
    pattern_source: str
    matched_text: str


@dataclass
class PackageResult:
    """Return value from ContextPackager.package()."""

    context_package_id: str
    assembled_payload: str
    assembled_payload_hash: str


class SensitivityConfig:
    """Loads and validates the sensitivity deny-list configuration.

    Each sensitivity class may include an ``allowlist`` of strings that
    should NOT be treated as matches even though they match the regex.
    This prevents false positives like "Context Packager" or "New York"
    being redacted as personal names.
    """

    def __init__(self, config_path: str) -> None:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        self.default_action: str = raw.get("default_action_on_unclassifiable", "strip")
        self.patterns: list[SensitivityPattern] = []
        self.allowlists: dict[str, set[str]] = {}

        for cls_def in raw.get("sensitivity_classes", []):
            cls_name = cls_def["class"]
            action = cls_def["action"]
            hardcoded = cls_def.get("hardcoded", False)

            # Parse per-class allowlist (case-sensitive comparison)
            allowlist_entries = cls_def.get("allowlist", [])
            if allowlist_entries:
                self.allowlists[cls_name] = set(allowlist_entries)

            for pattern_str in cls_def.get("patterns", []):
                try:
                    compiled = re.compile(pattern_str)
                except re.error as exc:
                    logger.warning(
                        "Invalid regex in sensitivity config class=%s pattern=%s: %s",
                        cls_name,
                        pattern_str,
                        exc,
                    )
                    continue

                self.patterns.append(
                    SensitivityPattern(
                        regex=compiled,
                        sensitivity_class=cls_name,
                        action=action,
                        hardcoded=hardcoded,
                        pattern_source=pattern_str,
                    )
                )

        logger.info(
            "Loaded sensitivity config: %d patterns, %d allowlisted classes, default_action=%s",
            len(self.patterns),
            len(self.allowlists),
            self.default_action,
        )


def _generalize_location(match_text: str) -> str:
    """Generalize a zip code: 31543 -> 315XX, 31543-1234 -> 315XX-XXXX."""
    if "-" in match_text:
        base, ext = match_text.split("-", 1)
        return base[:3] + "XX" + "-" + "X" * len(ext)
    return match_text[:3] + "XX"


def _generalize_date(match_text: str) -> str:
    """Generalize a date to just the year.

    Handles: MM/DD/YYYY, M/D/YY, YYYY-MM-DD
    """
    if "-" in match_text and len(match_text) == 10 and match_text[4] == "-":
        # ISO format: YYYY-MM-DD -> YYYY
        return match_text[:4]

    if "/" in match_text:
        parts = match_text.split("/")
        if len(parts) == 3:
            year_part = parts[2]
            if len(year_part) == 2:
                # M/D/YY -> convert to full year
                year_int = int(year_part)
                return str(2000 + year_int) if year_int < 70 else str(1900 + year_int)
            return year_part

    return match_text


GENERALIZERS = {
    "location": _generalize_location,
    "date": _generalize_date,
}


class ContextPackager:
    """Inline deny-list filter for cloud-bound payloads."""

    def __init__(
        self,
        config: SensitivityConfig,
        audit_client: AuditLogClient,
        context_store_dir: str = "/var/drnt/context",
    ) -> None:
        self._config = config
        self._audit = audit_client
        self._context_store_dir = context_store_dir
        os.makedirs(self._context_store_dir, exist_ok=True)

    def _scan(self, text: str) -> list[DetectedSpan]:
        """Scan text against all sensitivity patterns. Returns detected spans."""
        spans: list[DetectedSpan] = []

        for pattern in self._config.patterns:
            allowlist = self._config.allowlists.get(pattern.sensitivity_class, set())
            for match in pattern.regex.finditer(text):
                matched = match.group()
                if matched in allowlist:
                    logger.debug(
                        "Allowlist skip: class=%s matched=%r",
                        pattern.sensitivity_class, matched,
                    )
                    continue
                spans.append(
                    DetectedSpan(
                        start=match.start(),
                        end=match.end(),
                        sensitivity_class=pattern.sensitivity_class,
                        action=pattern.action,
                        pattern_source=pattern.pattern_source,
                        matched_text=matched,
                    )
                )

        return spans

    def _merge_overlapping_spans(self, spans: list[DetectedSpan]) -> list[DetectedSpan]:
        """Merge overlapping spans, keeping the most restrictive action."""
        if not spans:
            return []

        # Sort by start position, then by action precedence (most restrictive first)
        spans.sort(key=lambda s: (s.start, ACTION_PRECEDENCE.get(s.action, 2)))

        merged: list[DetectedSpan] = [spans[0]]
        for span in spans[1:]:
            prev = merged[-1]
            if span.start < prev.end:
                # Overlapping — extend and keep most restrictive action
                if ACTION_PRECEDENCE.get(span.action, 2) < ACTION_PRECEDENCE.get(prev.action, 2):
                    prev.action = span.action
                    prev.sensitivity_class = span.sensitivity_class
                    prev.pattern_source = span.pattern_source
                prev.end = max(prev.end, span.end)
                prev.matched_text = prev.matched_text  # Keep original for audit
            else:
                merged.append(span)

        return merged

    def _apply_transform(self, span: DetectedSpan) -> str:
        """Apply the configured action to a detected span."""
        if span.action == "strip":
            return f"[REDACTED:{span.sensitivity_class}]"
        elif span.action == "generalize":
            generalizer = GENERALIZERS.get(span.sensitivity_class)
            if generalizer:
                return generalizer(span.matched_text)
            return f"[REDACTED:{span.sensitivity_class}]"
        return span.matched_text

    def _transform_text(
        self, text: str, spans: list[DetectedSpan]
    ) -> tuple[str, list[DetectedSpan]]:
        """Apply all transformations and return (transformed_text, applied_spans)."""
        if not spans:
            return text, []

        merged = self._merge_overlapping_spans(spans)
        result_parts: list[str] = []
        last_end = 0

        for span in merged:
            result_parts.append(text[last_end : span.start])
            result_parts.append(self._apply_transform(span))
            last_end = span.end

        result_parts.append(text[last_end:])
        return "".join(result_parts), merged

    async def package(
        self,
        job_id: str,
        raw_input: str,
        target_model: str,
        route_id: str,
        capability_id: str,
        wal_level: int = 0,
    ) -> PackageResult:
        """Scan, strip, assemble, persist, and audit a cloud-bound payload."""
        from events import event_context_packaged, event_context_strip_detail

        context_package_id = _uuid7()
        field_id = _uuid7()

        # Scan for sensitive content
        raw_spans = self._scan(raw_input)
        assembled_payload, applied_spans = self._transform_text(raw_input, raw_spans)

        # Determine classification status for the field
        if applied_spans:
            sensitivity_class = applied_spans[0].sensitivity_class
            classification_status = "matched"
        else:
            sensitivity_class = "none"
            classification_status = "safe_none"

        assembled_payload_hash = _sha256(assembled_payload)
        token_estimate = len(assembled_payload) // 4

        # Build the context object
        context_object = {
            "context_package_id": context_package_id,
            "job_id": job_id,
            "governing_capability_id": capability_id,
            "governing_wal_level": wal_level,
            "context_package_wal_level": wal_level,
            "effective_packaging_level": min(wal_level, wal_level),
            "target_model": target_model,
            "route_id": route_id,
            "task_specification": {
                "request_category": None,
                "parsed_intent": None,
                "output_format": None,
                "prompt_template_id": None,
                "prompt_template_version": None,
            },
            "retrieved_candidates": [],
            "eligible_context_fields": [
                {
                    "field_id": field_id,
                    "field_name": "user_input",
                    "source_type": "user_input",
                    "source_id": None,
                    "sensitivity_class": sensitivity_class,
                    "classification_status": classification_status,
                    "shareability": "cloud_eligible",
                    "confidence": 1.0,
                    "content": assembled_payload,
                    "content_hash": assembled_payload_hash,
                }
            ],
            "assembled_payload": assembled_payload,
            "assembled_payload_hash": assembled_payload_hash,
            "token_estimate": token_estimate,
            "created_at": _now_iso(),
        }

        # Persist context object
        context_json = json.dumps(context_object, separators=(",", ":"))
        context_object_hash = _sha256(context_json)

        context_file = os.path.join(
            self._context_store_dir, f"{context_package_id}.json"
        )
        with open(context_file, "w", encoding="utf-8") as f:
            f.write(context_json)

        # Determine field lists for audit
        fields_stripped = [field_id] if applied_spans else []
        fields_included = [field_id] if not applied_spans else []

        # Emit context.packaged audit event
        packaged_event = event_context_packaged(
            context_package_id=context_package_id,
            job_id=job_id,
            fields_included=fields_included,
            fields_stripped=fields_stripped,
            outbound_token_estimate=token_estimate,
            assembled_payload_hash=assembled_payload_hash,
            context_object_hash=context_object_hash,
        )
        await self._audit.emit_durable(packaged_event)

        # Emit context.strip_detail for each transformation
        for span in applied_spans:
            strip_event = event_context_strip_detail(
                context_package_id=context_package_id,
                job_id=job_id,
                field_id=field_id,
                field_name="user_input",
                span_start=span.start,
                span_end=span.end,
                original_class=span.sensitivity_class,
                action="stripped" if span.action == "strip" else "generalized",
                reason=f"Matched {span.sensitivity_class} pattern: {span.pattern_source}",
            )
            await self._audit.emit_durable(strip_event)

        logger.info(
            "Context packaged: pkg=%s job=%s spans_transformed=%d",
            context_package_id,
            job_id,
            len(applied_spans),
        )

        return PackageResult(
            context_package_id=context_package_id,
            assembled_payload=assembled_payload,
            assembled_payload_hash=assembled_payload_hash,
        )
