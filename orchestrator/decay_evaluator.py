"""WAL Temporal Decay evaluator per Spec 7 Part 2.

Decay is not punitive — it does not record failures or contribute
to the 3-failures-in-24-hours demotion trigger. It is a separate
mechanism with trigger type "temporal_decay".

Evaluation runs:
- Once daily (via scheduled task)
- At startup
- On extended downtime: multiple passes until all capabilities
  reach their warranted level
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from capability_state import CapabilityStateManager
from events import event_wal_demoted_temporal_decay

logger = logging.getLogger(__name__)


@dataclass
class DecayPolicy:
    """Per-WAL-level decay parameters."""
    window_days: int
    min_outcomes: int


@dataclass
class DecayAction:
    """Result of a single decay demotion."""
    capability_id: str
    from_level: int
    to_level: int
    outcomes_in_window: int
    window_days: int
    min_required: int


@dataclass
class DecayConfig:
    """System-wide decay configuration with optional per-capability overrides."""
    system_defaults: dict[int, DecayPolicy]  # WAL level -> policy
    capability_overrides: dict[str, dict[int, DecayPolicy]] = field(
        default_factory=dict
    )  # capability_id -> WAL level -> policy
    enabled: bool = True


# WAL level keys in config JSON mapped to integer levels
_LEVEL_KEY_MAP = {"wal_1": 1, "wal_2": 2, "wal_3": 3}


def load_decay_config(config_data: dict[str, Any]) -> DecayConfig:
    """Load and validate decay configuration from capabilities config data.

    If the ``decay_policy`` key is entirely absent, decay is disabled.
    This is a deliberate choice logged at startup.

    Raises ``ValueError`` on invalid configuration.
    """
    if "decay_policy" not in config_data:
        logger.info("decay_policy key absent from config — temporal decay disabled")
        return DecayConfig(system_defaults={}, enabled=False)

    raw_policy = config_data["decay_policy"]
    system_defaults = _parse_policy_dict(raw_policy, "system_defaults")

    # Per-capability overrides
    capability_overrides: dict[str, dict[int, DecayPolicy]] = {}
    for cap_id, cap_policy_raw in config_data.get("decay_overrides", {}).items():
        override = _parse_policy_dict(cap_policy_raw, f"override[{cap_id}]")
        # Validate overrides are stricter than system defaults
        for level, policy in override.items():
            default = system_defaults.get(level)
            if default is None:
                continue
            if policy.window_days > default.window_days:
                raise ValueError(
                    f"Override for {cap_id} WAL-{level}: window_days "
                    f"({policy.window_days}) exceeds system default "
                    f"({default.window_days}) — overrides must be stricter"
                )
            if policy.min_outcomes < default.min_outcomes:
                raise ValueError(
                    f"Override for {cap_id} WAL-{level}: min_outcomes "
                    f"({policy.min_outcomes}) is less than system default "
                    f"({default.min_outcomes}) — overrides must be stricter"
                )
        capability_overrides[cap_id] = override

    return DecayConfig(
        system_defaults=system_defaults,
        capability_overrides=capability_overrides,
        enabled=True,
    )


def _parse_policy_dict(
    raw: dict[str, Any], label: str
) -> dict[int, DecayPolicy]:
    """Parse a WAL-level keyed policy dict, validating values."""
    result: dict[int, DecayPolicy] = {}
    for key, level in _LEVEL_KEY_MAP.items():
        if key not in raw:
            continue
        entry = raw[key]
        window = entry.get("window_days", 0)
        minimum = entry.get("min_outcomes", 0)
        if window <= 0:
            raise ValueError(
                f"{label} {key}: window_days must be > 0, got {window}"
            )
        if minimum <= 0:
            raise ValueError(
                f"{label} {key}: min_outcomes must be > 0, got {minimum}"
            )
        result[level] = DecayPolicy(window_days=window, min_outcomes=minimum)
    return result


class DecayEvaluator:
    """Evaluates WAL temporal decay per Spec 7 Part 2."""

    def __init__(self, config: DecayConfig, state_manager: CapabilityStateManager):
        self._config = config
        self._state = state_manager

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    async def evaluate(self, audit_client: Any) -> list[DecayAction]:
        """Evaluate all capabilities for temporal decay.

        For each capability at WAL-1 or above:
        1. Get effective decay policy (per-capability override or system default)
        2. Count evaluable outcomes in the ring buffer within the activity window
        3. If count < minimum, emit event and demote by ONE level

        Returns list of DecayAction results.
        """
        if not self._config.enabled:
            return []

        actions: list[DecayAction] = []
        now = datetime.now(timezone.utc)

        all_caps = self._state.get_all()
        for cap_id, cap_state in all_caps.items():
            current_level = cap_state.get("effective_wal_level", 0)
            if current_level < 1:
                continue

            policy = self._get_policy(cap_id, current_level)
            if policy is None:
                continue

            outcomes = self._state.get_outcomes(cap_id)
            cutoff = now - timedelta(days=policy.window_days)
            in_window = self._count_outcomes_in_window(outcomes, cutoff)

            if in_window >= policy.min_outcomes:
                continue

            # Determine last outcome timestamp
            last_ts = self._last_outcome_timestamp(outcomes)

            # Emit audit event
            event = event_wal_demoted_temporal_decay(
                capability_id=cap_id,
                from_level=current_level,
                window_days=policy.window_days,
                outcomes_in_window=in_window,
                min_required=policy.min_outcomes,
                last_outcome_timestamp=last_ts,
            )
            await audit_client.emit_durable(event)

            # Demote by one level and reset counters
            to_level = current_level - 1
            self._state.demote(cap_id, to_level)
            self._state.reset_counters(cap_id)

            actions.append(DecayAction(
                capability_id=cap_id,
                from_level=current_level,
                to_level=to_level,
                outcomes_in_window=in_window,
                window_days=policy.window_days,
                min_required=policy.min_outcomes,
            ))

        return actions

    # ------------------------------------------------------------------
    # Multi-pass for extended downtime
    # ------------------------------------------------------------------

    async def evaluate_startup(self, audit_client: Any) -> list[DecayAction]:
        """Run evaluate() in a loop until no more demotions occur.

        A WAL-2 capability idle for 120 days demotes to WAL-1 on first
        pass, then to WAL-0 on second pass. Prevents elevated privileges
        persisting after extended outage.
        """
        all_actions: list[DecayAction] = []
        while True:
            actions = await self.evaluate(audit_client)
            if not actions:
                break
            all_actions.extend(actions)
        return all_actions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_policy(self, capability_id: str, wal_level: int) -> DecayPolicy | None:
        """Get effective policy: per-capability override or system default."""
        overrides = self._config.capability_overrides.get(capability_id)
        if overrides and wal_level in overrides:
            return overrides[wal_level]
        return self._config.system_defaults.get(wal_level)

    @staticmethod
    def _count_outcomes_in_window(
        outcomes: list[dict], cutoff: datetime
    ) -> int:
        """Count outcomes whose timestamps fall within the window (inclusive)."""
        count = 0
        for outcome in outcomes:
            ts_str = outcome.get("timestamp")
            if ts_str is None:
                continue
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts >= cutoff:
                count += 1
        return count

    @staticmethod
    def _last_outcome_timestamp(outcomes: list[dict]) -> str | None:
        """Return the most recent outcome timestamp, or None if empty."""
        if not outcomes:
            return None
        latest: datetime | None = None
        latest_str: str | None = None
        for outcome in outcomes:
            ts_str = outcome.get("timestamp")
            if ts_str is None:
                continue
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if latest is None or ts > latest:
                latest = ts
                latest_str = ts_str
        return latest_str
