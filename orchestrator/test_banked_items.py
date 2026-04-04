"""Tests for Banked Items 3, 5, and 6.

Banked Item 3 — Egress fallback policy: egress.fallback_to_local event
Banked Item 5 — Name pattern false positives: allowlist
Banked Item 6 — Generalization output verified end-to-end in assembled payload
"""

from __future__ import annotations

import hashlib
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from context_packager import ContextPackager, SensitivityConfig
from events import event_egress_fallback_to_local
from registry import EgressRegistry, EgressRoute


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_sensitivity_config(tmp_path, *, allowlist=None):
    """Build a sensitivity config file, optionally with a name allowlist."""
    name_class = {
        "class": "name",
        "hardcoded": False,
        "action": "strip",
        "patterns": [r"\b[A-Z][a-z]+\s[A-Z][a-z]+\b"],
    }
    if allowlist is not None:
        name_class["allowlist"] = allowlist

    cfg = {
        "sensitivity_classes": [
            {
                "class": "credential",
                "hardcoded": True,
                "action": "strip",
                "patterns": [
                    r"(?i)(api[_-]?key|api[_-]?secret|access[_-]?token|bearer\s+)[=:\s]+\S+",
                    r"sk-[a-zA-Z0-9]{20,}",
                ],
            },
            name_class,
            {
                "class": "financial",
                "hardcoded": False,
                "action": "strip",
                "patterns": [
                    r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b",
                    r"\b\d{3}-\d{2}-\d{4}\b",
                ],
            },
            {
                "class": "location",
                "hardcoded": False,
                "action": "generalize",
                "patterns": [r"\b\d{5}(-\d{4})?\b"],
            },
            {
                "class": "date",
                "hardcoded": False,
                "action": "generalize",
                "patterns": [
                    r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
                    r"\b\d{4}-\d{2}-\d{2}\b",
                ],
            },
            {"class": "custom", "hardcoded": False, "action": "strip", "patterns": []},
        ],
        "default_action_on_unclassifiable": "strip",
    }
    p = tmp_path / "sensitivity.json"
    p.write_text(json.dumps(cfg))
    return SensitivityConfig(str(p))


@pytest.fixture
def sensitivity_config_with_allowlist(tmp_path):
    return _make_sensitivity_config(
        tmp_path,
        allowlist=[
            "Context Packager",
            "New York",
            "Google Chrome",
            "Machine Learning",
            "Apple Watch",
            "Docker Desktop",
        ],
    )


@pytest.fixture
def sensitivity_config_no_allowlist(tmp_path):
    return _make_sensitivity_config(tmp_path, allowlist=None)


@pytest.fixture
def mock_audit():
    c = MagicMock()
    c.emit_durable = AsyncMock(return_value=True)
    return c


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "context"
    d.mkdir()
    return str(d)


@pytest.fixture
def packager_with_allowlist(sensitivity_config_with_allowlist, mock_audit, store):
    return ContextPackager(
        config=sensitivity_config_with_allowlist,
        audit_client=mock_audit,
        context_store_dir=store,
    )


@pytest.fixture
def packager_no_allowlist(sensitivity_config_no_allowlist, mock_audit, store):
    return ContextPackager(
        config=sensitivity_config_no_allowlist,
        audit_client=mock_audit,
        context_store_dir=store,
    )


def _all_events(mock_audit):
    return [c[0][0] for c in mock_audit.emit_durable.call_args_list]


def _strip_events(mock_audit):
    return [e for e in _all_events(mock_audit) if e["event_type"] == "context.strip_detail"]


# ===========================================================================
# BANKED ITEM 3 — Egress Fallback Policy
# ===========================================================================


class TestEgressFallbackEvent:
    """egress.fallback_to_local event builder and structure."""

    def test_event_structure(self):
        evt = event_egress_fallback_to_local(
            job_id="job-100",
            blocked_route_ids=["claude-sonnet-default"],
            classifier_routing="local",
            capability_id="route.cloud.claude",
        )
        assert evt["event_type"] == "egress.fallback_to_local"
        assert evt["job_id"] == "job-100"
        assert evt["capability_id"] == "route.cloud.claude"
        assert evt["payload"]["blocked_route_ids"] == ["claude-sonnet-default"]
        assert evt["payload"]["classifier_routing"] == "local"
        assert "source_event_id" in evt
        assert "timestamp" in evt

    def test_event_multiple_blocked_routes(self):
        evt = event_egress_fallback_to_local(
            job_id="job-101",
            blocked_route_ids=["claude-sonnet-default", "openai-gpt4o-default"],
            classifier_routing="local",
            capability_id="route.multi",
        )
        assert len(evt["payload"]["blocked_route_ids"]) == 2


class TestCheckBlockedCloudRoutes:
    """JobManager._check_blocked_cloud_routes returns disabled cloud route_ids."""

    def _make_registry(self, tmp_path, routes):
        p = tmp_path / "egress.json"
        p.write_text(json.dumps({"routes": routes}))
        reg = EgressRegistry(config_path=str(p))
        reg.load()
        return reg

    def _base_route(self, route_id, provider, enabled, capabilities):
        return {
            "route_id": route_id,
            "provider": provider,
            "endpoint_url": "http://example.com",
            "model_string": "*",
            "allowed_capabilities": capabilities,
            "auth": {"method": "none", "secret_ref": None},
            "constraints": {"rate_limit_rpm": 60},
            "health": {
                "check_interval_seconds": 300,
                "timeout_ms": 5000,
                "consecutive_failures_before_down": 3,
            },
            "enabled": enabled,
        }

    def test_no_blocked_routes(self, tmp_path, mock_audit):
        from job_manager import JobManager

        reg = self._make_registry(tmp_path, [
            self._base_route("claude-default", "anthropic", True, ["route.cloud.claude"]),
            self._base_route("ollama-local", "ollama", True, ["route.local"]),
        ])
        jm = JobManager(audit_client=mock_audit, egress_registry=reg)
        assert jm._check_blocked_cloud_routes("route.cloud.claude") == []

    def test_one_blocked_cloud_route(self, tmp_path, mock_audit):
        from job_manager import JobManager

        reg = self._make_registry(tmp_path, [
            self._base_route("claude-default", "anthropic", False, ["route.cloud.claude"]),
            self._base_route("ollama-local", "ollama", True, ["route.local"]),
        ])
        jm = JobManager(audit_client=mock_audit, egress_registry=reg)
        blocked = jm._check_blocked_cloud_routes("route.cloud.claude")
        assert blocked == ["claude-default"]

    def test_ignores_disabled_ollama_route(self, tmp_path, mock_audit):
        """Disabled Ollama routes should not appear as blocked cloud routes."""
        from job_manager import JobManager

        reg = self._make_registry(tmp_path, [
            self._base_route("ollama-local", "ollama", False, ["route.local"]),
        ])
        jm = JobManager(audit_client=mock_audit, egress_registry=reg)
        assert jm._check_blocked_cloud_routes("route.local") == []

    def test_only_reports_routes_with_matching_capability(self, tmp_path, mock_audit):
        from job_manager import JobManager

        reg = self._make_registry(tmp_path, [
            self._base_route("claude-default", "anthropic", False, ["route.cloud.claude"]),
            self._base_route("openai-default", "openai", False, ["route.cloud.openai"]),
        ])
        jm = JobManager(audit_client=mock_audit, egress_registry=reg)
        blocked = jm._check_blocked_cloud_routes("route.cloud.claude")
        assert blocked == ["claude-default"]
        assert "openai-default" not in blocked

    def test_no_registry_returns_empty(self, mock_audit):
        from job_manager import JobManager

        jm = JobManager(audit_client=mock_audit, egress_registry=None)
        assert jm._check_blocked_cloud_routes("route.cloud.claude") == []


# ===========================================================================
# BANKED ITEM 5 — Name Pattern False Positives (Allowlist)
# ===========================================================================


class TestNameAllowlist:
    """Name pattern allowlist prevents false positive redaction."""

    @pytest.mark.asyncio
    async def test_allowlisted_terms_not_redacted(
        self, packager_with_allowlist, mock_audit
    ):
        """Known non-name capitalized phrases pass through unredacted."""
        raw = (
            "the Context Packager runs on Docker Desktop and "
            "uses Machine Learning to classify input via Google Chrome."
        )
        result = await packager_with_allowlist.package(
            job_id="job-al-1", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )
        assert "Context Packager" in result.assembled_payload
        assert "Docker Desktop" in result.assembled_payload
        assert "Machine Learning" in result.assembled_payload
        assert "Google Chrome" in result.assembled_payload
        assert "[REDACTED:name]" not in result.assembled_payload

    @pytest.mark.asyncio
    async def test_real_names_still_redacted_with_allowlist(
        self, packager_with_allowlist, mock_audit
    ):
        """Actual personal names are still redacted even with allowlist active."""
        raw = "Send the report to John Smith at the New York office."
        result = await packager_with_allowlist.package(
            job_id="job-al-2", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )
        assert "John Smith" not in result.assembled_payload
        assert "[REDACTED:name]" in result.assembled_payload
        # New York should survive
        assert "New York" in result.assembled_payload

    @pytest.mark.asyncio
    async def test_without_allowlist_false_positives_occur(
        self, packager_no_allowlist, mock_audit
    ):
        """Without an allowlist, 'Context Packager' IS redacted (proving the fix works)."""
        raw = "The Context Packager filters sensitive data."
        result = await packager_no_allowlist.package(
            job_id="job-al-3", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )
        assert "Context Packager" not in result.assembled_payload
        assert "[REDACTED:name]" in result.assembled_payload

    @pytest.mark.asyncio
    async def test_allowlist_is_case_sensitive(
        self, packager_with_allowlist, mock_audit
    ):
        """Allowlist comparison is exact-match (case-sensitive)."""
        raw = "Check the Apple Watch for notifications."
        result = await packager_with_allowlist.package(
            job_id="job-al-4", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )
        assert "Apple Watch" in result.assembled_payload

    @pytest.mark.asyncio
    async def test_strip_events_not_emitted_for_allowlisted(
        self, packager_with_allowlist, mock_audit
    ):
        """No context.strip_detail events for allowlisted matches."""
        raw = "Machine Learning on Docker Desktop"
        await packager_with_allowlist.package(
            job_id="job-al-5", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )
        strips = _strip_events(mock_audit)
        assert len(strips) == 0

    def test_config_loads_allowlist(self, sensitivity_config_with_allowlist):
        """SensitivityConfig parses the allowlist from JSON."""
        assert "name" in sensitivity_config_with_allowlist.allowlists
        assert "Context Packager" in sensitivity_config_with_allowlist.allowlists["name"]
        assert len(sensitivity_config_with_allowlist.allowlists["name"]) == 6

    def test_config_no_allowlist_key(self, sensitivity_config_no_allowlist):
        """No allowlist key means empty allowlists dict for that class."""
        assert sensitivity_config_no_allowlist.allowlists.get("name") is None


# ===========================================================================
# BANKED ITEM 6 — Generalization Output Verified End-to-End
# ===========================================================================


class TestGeneralizationEndToEnd:
    """Verify generalization transforms produce correct output in the
    assembled payload stored in the context object JSON — not just that
    the audit event fired.
    """

    @pytest.mark.asyncio
    async def test_zip_code_generalized_in_payload(
        self, packager_with_allowlist, mock_audit, store
    ):
        """5-digit zip: 31543 -> 315XX in assembled_payload."""
        raw = "Ship the package to zip code 31543 please."
        result = await packager_with_allowlist.package(
            job_id="job-gen-zip5", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        # Check the returned payload
        assert "31543" not in result.assembled_payload
        assert "315XX" in result.assembled_payload

        # Verify the persisted context object JSON
        ctx_path = os.path.join(store, f"{result.context_package_id}.json")
        with open(ctx_path, "r", encoding="utf-8") as f:
            ctx = json.loads(f.read())

        assert "31543" not in ctx["assembled_payload"]
        assert "315XX" in ctx["assembled_payload"]

    @pytest.mark.asyncio
    async def test_zip_plus4_generalized_in_payload(
        self, packager_with_allowlist, mock_audit, store
    ):
        """ZIP+4: 31543-1234 -> 315XX-XXXX in assembled_payload."""
        raw = "Address zip is 31543-1234 for delivery."
        result = await packager_with_allowlist.package(
            job_id="job-gen-zip9", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        assert "31543-1234" not in result.assembled_payload
        assert "315XX-XXXX" in result.assembled_payload

        ctx_path = os.path.join(store, f"{result.context_package_id}.json")
        with open(ctx_path, "r", encoding="utf-8") as f:
            ctx = json.loads(f.read())
        assert "315XX-XXXX" in ctx["assembled_payload"]

    @pytest.mark.asyncio
    async def test_iso_date_generalized_in_payload(
        self, packager_with_allowlist, mock_audit, store
    ):
        """ISO date: 2026-03-30 -> 2026 in assembled_payload."""
        raw = "The incident occurred on 2026-03-30 at noon."
        result = await packager_with_allowlist.package(
            job_id="job-gen-iso", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        assert "2026-03-30" not in result.assembled_payload
        assert "2026" in result.assembled_payload

        ctx_path = os.path.join(store, f"{result.context_package_id}.json")
        with open(ctx_path, "r", encoding="utf-8") as f:
            ctx = json.loads(f.read())
        assert "2026-03-30" not in ctx["assembled_payload"]
        assert "2026" in ctx["assembled_payload"]

    @pytest.mark.asyncio
    async def test_us_date_generalized_in_payload(
        self, packager_with_allowlist, mock_audit, store
    ):
        """US date: 03/15/1985 -> 1985 in assembled_payload."""
        raw = "Born on 03/15/1985 in Georgia."
        result = await packager_with_allowlist.package(
            job_id="job-gen-us", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        assert "03/15/1985" not in result.assembled_payload
        assert "1985" in result.assembled_payload

        ctx_path = os.path.join(store, f"{result.context_package_id}.json")
        with open(ctx_path, "r", encoding="utf-8") as f:
            ctx = json.loads(f.read())
        assert "1985" in ctx["assembled_payload"]

    @pytest.mark.asyncio
    async def test_short_year_date_generalized_in_payload(
        self, packager_with_allowlist, mock_audit, store
    ):
        """Short year date: 3/30/26 -> 2026 in assembled_payload."""
        raw = "Meeting rescheduled to 3/30/26 per request."
        result = await packager_with_allowlist.package(
            job_id="job-gen-short", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        assert "3/30/26" not in result.assembled_payload
        assert "2026" in result.assembled_payload

        ctx_path = os.path.join(store, f"{result.context_package_id}.json")
        with open(ctx_path, "r", encoding="utf-8") as f:
            ctx = json.loads(f.read())
        assert "2026" in ctx["assembled_payload"]

    @pytest.mark.asyncio
    async def test_payload_hash_matches_generalized_content(
        self, packager_with_allowlist, mock_audit, store
    ):
        """The assembled_payload_hash matches SHA-256 of the generalized text."""
        raw = "Event at 31543 on 2026-03-30."
        result = await packager_with_allowlist.package(
            job_id="job-gen-hash", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        expected_hash = hashlib.sha256(
            result.assembled_payload.encode("utf-8")
        ).hexdigest()
        assert result.assembled_payload_hash == expected_hash

        # Also verify in the persisted context object
        ctx_path = os.path.join(store, f"{result.context_package_id}.json")
        with open(ctx_path, "r", encoding="utf-8") as f:
            ctx = json.loads(f.read())
        assert ctx["assembled_payload_hash"] == expected_hash

    @pytest.mark.asyncio
    async def test_generalized_payload_not_malformed(
        self, packager_with_allowlist, mock_audit, store
    ):
        """Surrounding text is preserved correctly around generalized values."""
        raw = "Deliver to 31543 before 2026-03-30 deadline."
        result = await packager_with_allowlist.package(
            job_id="job-gen-intact", raw_input=raw,
            target_model="m", route_id="r", capability_id="c",
        )

        # Should read like: "Deliver to 315XX before 2026 deadline."
        payload = result.assembled_payload
        assert payload.startswith("Deliver to ")
        assert "315XX" in payload
        assert "2026" in payload
        assert payload.endswith(" deadline.")
        # No double spaces, no dangling characters
        assert "  " not in payload
