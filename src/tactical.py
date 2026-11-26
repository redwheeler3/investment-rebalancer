"""Tactical defensive deployment — drawdown-based dynamic target adjustment.

Implements a regime state machine that shifts the fixed/equity allocation
split based on portfolio drawdown from a Reference High. The system deploys
fixed-income assets into equities on the way down, and rebuilds the fixed
position on the way back up.

Key concepts:
- Reference High: At baseline, equals the ATH (tracks new highs). When
  deployed, freezes at the ATH value when deployment first triggered.
- Regime: Baseline or a configured deployment level determines the
  current fixed/equity split.
- Hysteresis: Different thresholds on the way down vs. up to avoid
  whipsawing near boundaries.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from src.history import household_date
from src.encrypted_state import read_encrypted_json, write_encrypted_json
from src.paths import get_tactical_state_file

# ══════════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════════


@dataclass
class TacticalConfig:
    """Tactical deployment configuration from settings.yaml."""

    baseline_fixed_pct: float
    fixed_composition: dict[str, float]  # symbol → ratio (sums to 1.0, parsed from %)
    deploy_thresholds: list[
        dict
    ]  # sorted by drawdown_pct descending (least negative first)
    recovery_thresholds: list[
        dict
    ]  # sorted by drawdown_pct ascending (most negative first)
    gic_account_types: tuple[str, ...] = ()
    gic_min_buy_cad: float = 5000

    @property
    def regimes(self):
        return ["baseline"] + [
            f"level_{i}" for i in range(1, len(self.deploy_thresholds) + 1)
        ]


@dataclass
class TacticalState:
    """Persisted tactical deployment state."""

    regime: str = "baseline"
    reference_high: float | None = None  # None at baseline (derived from ATH)
    reference_high_date: str | None = None
    last_transition_date: str | None = None


@dataclass
class TacticalPosture:
    """Computed tactical posture for display and target resolution."""

    regime: str
    fixed_pct: float
    equity_pct: float
    reference_high: float
    reference_high_date: str
    drawdown_from_reference_pct: float
    transition_occurred: bool = False
    previous_regime: str | None = None
    # Trigger info for display
    next_deploy_trigger: dict | None = None  # {drawdown_pct, fixed_pct, dollar_value}
    next_recovery_triggers: list[dict] = field(
        default_factory=list
    )  # [{drawdown_pct, fixed_pct, dollar_value}]


# ══════════════════════════════════════════════════════════════════
# State persistence
# ══════════════════════════════════════════════════════════════════


def load_tactical_state() -> TacticalState:
    """Load tactical state from disk, or return default baseline state."""
    state_file = get_tactical_state_file()
    if not state_file.exists():
        return TacticalState()

    data = read_encrypted_json(state_file)
    return TacticalState(
        regime=data.get("regime", "baseline"),
        reference_high=data.get("reference_high"),
        reference_high_date=data.get("reference_high_date"),
        last_transition_date=data.get("last_transition_date"),
    )


def save_tactical_state(state: TacticalState) -> None:
    """Persist tactical state to disk. Creates data/ directory if needed."""
    state_file = get_tactical_state_file()
    data = {"regime": state.regime}
    if state.reference_high is not None:
        data["reference_high"] = round(state.reference_high, 2)
    if state.reference_high_date is not None:
        data["reference_high_date"] = state.reference_high_date
    if state.last_transition_date is not None:
        data["last_transition_date"] = state.last_transition_date

    write_encrypted_json(state_file, data)


# ══════════════════════════════════════════════════════════════════
# Config parsing
# ══════════════════════════════════════════════════════════════════


def parse_tactical_config(raw: dict) -> TacticalConfig | None:
    """Parse tactical_deployment section from settings.yaml.

    Returns None if the section is missing or empty.
    """
    if not raw:
        return None

    baseline_fixed_pct = float(raw.get("baseline_fixed_pct", 20.0))

    # Parse fixed composition ratios
    fixed_composition = {}
    for symbol, ratio in raw.get("fixed_composition", {}).items():
        fixed_composition[str(symbol)] = float(ratio)

    if not 0 <= baseline_fixed_pct <= 100:
        raise ValueError("baseline_fixed_pct must be between 0 and 100.")
    if {"CAD", "USD"} & fixed_composition.keys():
        raise ValueError("Remove brokerage cash from fixed_composition.")
    if not fixed_composition or any(
        not math.isfinite(v) or v < 0 for v in fixed_composition.values()
    ):
        raise ValueError("Fixed composition needs finite, nonnegative weights.")

    # Normalize to ratios summing to 1.0 (config uses percentages summing to 100)
    total_ratio = sum(fixed_composition.values())
    if not math.isfinite(total_ratio) or total_ratio <= 0:
        raise ValueError("Fixed composition requires a positive total.")
    if abs(total_ratio - 1.0) > 0.001:
        fixed_composition = {
            symbol: ratio / total_ratio for symbol, ratio in fixed_composition.items()
        }

    # Parse deploy thresholds (going down) — sort so least negative first
    deploy_thresholds = []
    for entry in raw.get("deploy_thresholds", []):
        deploy_thresholds.append(
            {
                "drawdown_pct": float(entry["drawdown_pct"]),
                "fixed_pct": float(entry["fixed_pct"]),
            }
        )
    deploy_thresholds.sort(key=lambda t: t["drawdown_pct"], reverse=True)

    # Parse recovery thresholds (going up) — sort so most negative first
    recovery_thresholds = []
    for entry in raw.get("recovery_thresholds", []):
        recovery_thresholds.append(
            {
                "drawdown_pct": float(entry["drawdown_pct"]),
                "fixed_pct": float(entry["fixed_pct"]),
            }
        )
    recovery_thresholds.sort(key=lambda t: t["drawdown_pct"])

    levels = [baseline_fixed_pct] + [t["fixed_pct"] for t in deploy_thresholds]
    if any(not 0 <= lower < upper for upper, lower in zip(levels, levels[1:])):
        raise ValueError("Each deployment level must reduce fixed income.")
    deploy_points = [t["drawdown_pct"] for t in deploy_thresholds]
    if len(set(deploy_points)) != len(deploy_points) or any(
        not -100 <= d < 0 for d in deploy_points
    ):
        raise ValueError(
            "Deployment drawdowns must be distinct and between -100 and 0."
        )
    prior_drawdown, prior_fixed = -math.inf, -math.inf
    for threshold in recovery_thresholds:
        d, fixed = threshold["drawdown_pct"], threshold["fixed_pct"]
        if fixed not in levels[:-1] or not (
            math.isfinite(d) and d > prior_drawdown and fixed > prior_fixed
        ):
            raise ValueError(
                "Recovery thresholds must lead to successively higher configured fixed levels."
            )
        if d <= deploy_points[levels.index(fixed)]:
            raise ValueError(
                "Recovery must occur above the drawdown for entering the deeper level."
            )
        prior_drawdown, prior_fixed = d, fixed

    types = raw.get("gic_account_types", [])
    minimum = raw.get("gic_min_buy_cad", 5000)
    if not isinstance(types, list) or any(
        not isinstance(t, str) or not t.strip() or t != t.strip() for t in types
    ):
        raise ValueError("gic_account_types must list Questrade account type names.")
    if (
        type(minimum) not in (int, float)
        or not math.isfinite(minimum)
        or minimum < 0.01
        or round(minimum, 2) != minimum
    ):
        raise ValueError(
            "gic_min_buy_cad must be a positive CAD amount in whole cents."
        )
    return TacticalConfig(
        baseline_fixed_pct=baseline_fixed_pct,
        fixed_composition=fixed_composition,
        deploy_thresholds=deploy_thresholds,
        recovery_thresholds=recovery_thresholds,
        gic_account_types=tuple(types),
        gic_min_buy_cad=minimum,
    )


# ══════════════════════════════════════════════════════════════════
# Regime state machine
# ══════════════════════════════════════════════════════════════════


def _fixed_pct_for_regime(regime: str, config: TacticalConfig) -> float:
    """Return the fixed-income percentage for a given regime."""
    baseline_fixed = config.baseline_fixed_pct

    if regime == "baseline":
        return baseline_fixed

    return config.deploy_thresholds[config.regimes.index(regime) - 1]["fixed_pct"]


def _determine_regime(
    current_regime: str,
    drawdown_pct: float,
    config: TacticalConfig,
) -> str:
    """Determine the correct regime based on current drawdown and direction.

    Uses hysteresis: deploy thresholds going down, recovery thresholds going up.
    Jumps directly to the deepest qualifying level — if the portfolio drops 30%
    in one day, it moves straight to the appropriate deployment level in a
    single evaluation.
    """
    current_index = config.regimes.index(current_regime)

    # Check deploy thresholds (going deeper into drawdown)
    # These trigger moving to a MORE deployed state (less fixed).
    # Find the deepest level that qualifies (don't stop at first match).
    for index in range(len(config.deploy_thresholds), current_index, -1):
        if drawdown_pct <= config.deploy_thresholds[index - 1]["drawdown_pct"]:
            return config.regimes[index]

    # Check recovery thresholds (recovering from drawdown)
    # These trigger moving to a LESS deployed state (more fixed).
    # Find the most recovered (closest to baseline) level that qualifies.
    for threshold in reversed(config.recovery_thresholds):
        target_fixed = threshold["fixed_pct"]
        trigger_drawdown = threshold["drawdown_pct"]

        target_regime = _regime_for_fixed_pct(target_fixed, config)
        if target_regime is None:
            continue

        target_index = config.regimes.index(target_regime)

        if target_index < current_index and drawdown_pct >= trigger_drawdown:
            return target_regime

    return current_regime


def _regime_for_fixed_pct(fixed_pct: float, config: TacticalConfig) -> str | None:
    """Map a fixed_pct value to its corresponding regime name."""
    baseline_fixed = config.baseline_fixed_pct

    if fixed_pct == baseline_fixed:
        return "baseline"

    for i, threshold in enumerate(config.deploy_thresholds):
        if threshold["fixed_pct"] == fixed_pct:
            return f"level_{i + 1}"

    return None


# ══════════════════════════════════════════════════════════════════
# Main evaluation
# ══════════════════════════════════════════════════════════════════


def evaluate_tactical_posture(
    current_value: float,
    ath_value: float,
    ath_date: str,
    config: TacticalConfig,
    *,
    as_of: date | None = None,
) -> TacticalPosture:
    """Evaluate the tactical posture given current portfolio state.

    This is the main entry point. It:
    1. Loads the persisted state
    2. Determines the reference high
    3. Calculates drawdown from reference
    4. Evaluates regime transitions
    5. Persists state if a transition occurred
    6. Returns the full posture for display and target resolution

    Args:
        current_value: Current portfolio value in CAD.
        ath_value: All-time high value from portfolio history.
        ath_date: Date of the all-time high (ISO format).
        config: Tactical deployment configuration.

    Returns:
        TacticalPosture with regime, targets, and display info.
    """
    state = load_tactical_state()
    today = (as_of or household_date()).isoformat()

    # Determine reference high
    if state.regime == "baseline":
        # At baseline: reference high tracks ATH
        reference_high = ath_value
        reference_high_date = ath_date
    else:
        # Deployed: reference high is frozen in state
        reference_high = state.reference_high or ath_value
        reference_high_date = state.reference_high_date or ath_date

    # Calculate drawdown from reference
    if reference_high > 0:
        drawdown_pct = ((current_value - reference_high) / reference_high) * 100.0
    else:
        drawdown_pct = 0.0

    # Evaluate regime transition
    previous_regime = state.regime
    new_regime = _determine_regime(state.regime, drawdown_pct, config)
    transition_occurred = new_regime != previous_regime

    # Persist state if transition occurred
    if transition_occurred:
        if new_regime == "baseline":
            reference_high, reference_high_date = ath_value, ath_date
            drawdown_pct = (
                (current_value / ath_value - 1) * 100 if ath_value > 0 else 0.0
            )
            # Returning to baseline clears the frozen reference.
            state.regime = "baseline"
            state.reference_high = None
            state.reference_high_date = None
            state.last_transition_date = today
        elif previous_regime == "baseline":
            # Leaving baseline: freeze the current ATH as reference
            state.regime = new_regime
            state.reference_high = ath_value
            state.reference_high_date = ath_date
            state.last_transition_date = today
            # Update local reference for this evaluation
            reference_high = ath_value
            reference_high_date = ath_date
        else:
            # Moving between deployed levels
            state.regime = new_regime
            state.last_transition_date = today

        save_tactical_state(state)

    # Compute fixed/equity split for current regime
    fixed_pct = _fixed_pct_for_regime(new_regime, config)
    equity_pct = 100.0 - fixed_pct

    # Compute trigger info for display
    next_deploy_trigger = _next_deploy_trigger(new_regime, reference_high, config)
    next_recovery_triggers = _next_recovery_triggers(new_regime, reference_high, config)

    return TacticalPosture(
        regime=new_regime,
        fixed_pct=fixed_pct,
        equity_pct=equity_pct,
        reference_high=reference_high,
        reference_high_date=reference_high_date,
        drawdown_from_reference_pct=drawdown_pct,
        transition_occurred=transition_occurred,
        previous_regime=previous_regime if transition_occurred else None,
        next_deploy_trigger=next_deploy_trigger,
        next_recovery_triggers=next_recovery_triggers,
    )


# ══════════════════════════════════════════════════════════════════
# Trigger computation (for display)
# ══════════════════════════════════════════════════════════════════


def _next_deploy_trigger(
    current_regime: str,
    reference_high: float,
    config: TacticalConfig,
) -> dict | None:
    """Find the next deploy threshold below current regime."""
    current_index = config.regimes.index(current_regime)

    # Next deploy level is one deeper
    next_index = current_index + 1
    if next_index >= len(config.regimes):
        return None  # Already at max deployment

    # Find the deploy threshold for the next level
    threshold_index = next_index - 1  # deploy_thresholds[0] → level_1, etc.
    if threshold_index >= len(config.deploy_thresholds):
        return None

    threshold = config.deploy_thresholds[threshold_index]
    dollar_value = reference_high * (1.0 + threshold["drawdown_pct"] / 100.0)

    return {
        "drawdown_pct": threshold["drawdown_pct"],
        "fixed_pct": threshold["fixed_pct"],
        "dollar_value": dollar_value,
        "target_regime": config.regimes[next_index],
    }


def _next_recovery_triggers(
    current_regime: str,
    reference_high: float,
    config: TacticalConfig,
) -> list[dict]:
    """Find all recovery thresholds above current regime."""
    if current_regime == "baseline":
        return []

    current_fixed = _fixed_pct_for_regime(current_regime, config)
    triggers = []

    for threshold in config.recovery_thresholds:
        # Recovery thresholds that would move us to a less-deployed state
        if threshold["fixed_pct"] > current_fixed:
            target_regime = _regime_for_fixed_pct(threshold["fixed_pct"], config)
            dollar_value = reference_high * (1.0 + threshold["drawdown_pct"] / 100.0)
            triggers.append(
                {
                    "drawdown_pct": threshold["drawdown_pct"],
                    "fixed_pct": threshold["fixed_pct"],
                    "dollar_value": dollar_value,
                    "target_regime": target_regime or "baseline",
                }
            )

    return triggers


# ══════════════════════════════════════════════════════════════════
# Target resolution
# ══════════════════════════════════════════════════════════════════


def resolve_tactical_targets(
    targets: dict[str, float],
    posture: TacticalPosture,
    config: TacticalConfig,
) -> dict[str, float]:
    """Adjust portfolio targets based on the current tactical posture.

    - Fixed-income targets are set absolutely from fixed_composition × fixed_pct
    - Equity targets are scaled proportionally so everything sums to 100%

    Args:
        targets: Current resolved targets (after fx_target_rules).
        posture: The evaluated tactical posture.
        config: Tactical deployment configuration.

    Returns:
        New target dict with adjusted allocations.
    """
    fixed_symbols = set(config.fixed_composition.keys())

    if {"CAD", "USD"} & targets.keys():
        raise ValueError("Remove brokerage cash from targets.")
    equity_targets = {s: pct for s, pct in targets.items() if s not in fixed_symbols}
    equity_sum = sum(equity_targets.values())
    if equity_sum > 0:
        equity_targets = {
            symbol: pct * posture.equity_pct / equity_sum
            for symbol, pct in equity_targets.items()
        }

    # Compute fixed-income targets from composition ratios
    fixed_targets = {
        symbol: posture.fixed_pct * ratio
        for symbol, ratio in config.fixed_composition.items()
    }

    # Combine all targets
    result = {}
    result.update(equity_targets)
    result.update(fixed_targets)

    return result
