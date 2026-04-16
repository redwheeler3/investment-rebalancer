from dataclasses import dataclass
from typing import Any


@dataclass
class AllocationSnapshot:
    """A complete allocation view with derived drift and accuracy metrics."""

    allocations: dict
    drifts: dict
    accuracy: float


@dataclass
class RebalanceReportData:
    """All calculated data needed to render the rebalancing report."""

    current: AllocationSnapshot
    transient_alerts: list
    trades: list
    currency_conversions: list
    projected: AllocationSnapshot | None = None
    all_time_high: Any | None = None