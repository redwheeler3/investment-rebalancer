"""Tactical defensive deployment — drawdown-based dynamic target adjustment.

Implements a regime state machine that shifts the fixed/equity allocation
split based on portfolio drawdown from a Reference High. The system deploys
fixed-income assets into equities on the way down, and rebuilds the fixed
position on the way back up.

Key concepts:
- Reference High: At baseline, equals the ATH (tracks new highs). When
  deployed, freezes at the ATH value when deployment first triggered.
- Regime: One of baseline, level_1, level_2, level_3 — determines the
  current fixed/equity split.
- Hysteresis: Different thresholds on the way down vs. up to avoid
  whipsawing near boundaries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from src.paths import get_tactical_state_file


# ══════════════════════════════════════════════════════════════════
# Data types
# ══════════════════════════════════════════════════════════════════


REGIMES = ("baseline", "level_1", "level_2", "level_3")


@dataclass
class TacticalConfig:
    """Tactical deployment configuration from settings.yaml."""

    baseline_fixed_pct: float
    fixed_composition: dict[str, float]  # symbol → ratio (sums to 1.0, parsed from %)
    deploy_thresholds: list[dict]  # sorted by drawdown_pct descending (least negative first)
    recovery_thresholds: list[dict]  # sorted by drawdown_pct ascending (most negative first)


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
    next_recovery_triggers: list[dict] = field(default_factory=list)  # [{drawdown_pct, fixed_pct, dollar_value}]


# ══════════════════════════════════════════════════════════════════
# State persistence
# ══════════════════════════════════════════════════════════════════


def load_tactical_state() -> TacticalState:
    """Load tactical state from disk, or return default baseline state."""
    state_file = get_tactical_state_file()
    if not state_file.exists():
        return TacticalState()

    try:
        with open(state_file, "r") as f:
            data = json.load(f)
        return TacticalState(
            regime=data.get("regime", "baseline"),
            reference_high=data.get("reference_high"),
            reference_high_date=data.get("reference_high_date"),
            last_transition_date=data.get("last_transition_date"),
        )
    except (json.JSONDecodeError, IOError):
        return TacticalState()


def save_tactical_state(state: TacticalState) -> None:
    """Persist tactical state to disk. Creates data/ directory if needed."""
    state_file = get_tactical_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)

    data = {"regime": state.regime}
    if state.reference_high is not None:
        data["reference_high"] = round(state.reference_high, 2)
    if state.reference_high_date is not None:
        data["reference_high_date"] = state.reference_high_date
    if state.last_transition_date is not None:
        data["last_transition_date"] = state.last_transition_date

    with open(state_file, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


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

    if not fixed_composition:
        return None

    # Normalize to ratios summing to 1.0 (config uses percentages summing to 100)
    total_ratio = sum(fixed_composition.values())
    if total_ratio > 0 and abs(total_ratio - 1.0) > 0.001:
        fixed_composition = {
            symbol: ratio / total_ratio
            for symbol, ratio in fixed_composition.items()
        }

    # Parse deploy thresholds (going down) — sort so least negative first
    deploy_thresholds = []
    for entry in raw.get("deploy_thresholds", []):
        deploy_thresholds.append({
            "drawdown_pct": float(entry["drawdown_pct"]),
            "fixed_pct": float(entry["fixed_pct"]),
        })
    deploy_thresholds.sort(key=lambda t: t["drawdown_pct"], reverse=True)

    # Parse recovery thresholds (going up) — sort so most negative first
    recovery_thresholds = []
    for entry in raw.get("recovery_thresholds", []):
        recovery_thresholds.append({
            "drawdown_pct": float(entry["drawdown_pct"]),
            "fixed_pct": float(entry["fixed_pct"]),
        })
    recovery_thresholds.sort(key=lambda t: t["drawdown_pct"])

    return TacticalConfig(
        baseline_fixed_pct=baseline_fixed_pct,
        fixed_composition=fixed_composition,
        deploy_thresholds=deploy_thresholds,
        recovery_thresholds=recovery_thresholds,
    )


# ══════════════════════════════════════════════════════════════════
# Regime state machine
# ══════════════════════════════════════════════════════════════════


def _fixed_pct_for_regime(regime: str, config: TacticalConfig) -> float:
    """Return the fixed-income percentage for a given regime."""
    baseline_fixed = config.baseline_fixed_pct

    if regime == "baseline":
        return baseline_fixed

    # Match regime to deploy threshold level
    regime_index = REGIMES.index(regime) - 1  # 0-indexed into deploy_thresholds
    if 0 <= regime_index < len(config.deploy_thresholds):
        return config.deploy_thresholds[regime_index]["fixed_pct"]

    return baseline_fixed


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
    current_index = REGIMES.index(current_regime)

    # Check deploy thresholds (going deeper into drawdown)
    # These trigger moving to a MORE deployed state (less fixed).
    # Find the deepest level that qualifies (don't stop at first match).
    best_deploy = None
    for threshold in config.deploy_thresholds:
        target_fixed = threshold["fixed_pct"]
        trigger_drawdown = threshold["drawdown_pct"]

        target_regime = _regime_for_fixed_pct(target_fixed, config)
        if target_regime is None:
            continue

        target_index = REGIMES.index(target_regime)

        if target_index > current_index and drawdown_pct <= trigger_drawdown:
            best_deploy = target_regime  # Keep going — later matches are deeper

    if best_deploy is not None:
        return best_deploy

    # Check recovery thresholds (recovering from drawdown)
    # These trigger moving to a LESS deployed state (more fixed).
    # Find the most recovered (closest to baseline) level that qualifies.
    for threshold in reversed(config.recovery_thresholds):
        target_fixed = threshold["fixed_pct"]
        trigger_drawdown = threshold["drawdown_pct"]

        target_regime = _regime_for_fixed_pct(target_fixed, config)
        if target_regime is None:
            continue

        target_index = REGIMES.index(target_regime)

        if target_index < current_index and drawdown_pct >= trigger_drawdown:
            return target_regime

    return current_regime


def _regime_for_fixed_pct(fixed_pct: float, config: TacticalConfig) -> str | None:
    """Map a fixed_pct value to its corresponding regime name."""
    baseline_fixed = config.baseline_fixed_pct

    if abs(fixed_pct - baseline_fixed) < 0.01:
        return "baseline"

    for i, threshold in enumerate(config.deploy_thresholds):
        if abs(threshold["fixed_pct"] - fixed_pct) < 0.01:
            regime_index = i + 1  # +1 because index 0 in REGIMES is "baseline"
            if regime_index < len(REGIMES):
                return REGIMES[regime_index]

    return None


# ══════════════════════════════════════════════════════════════════
# Main evaluation
# ══════════════════════════════════════════════════════════════════


def evaluate_tactical_posture(
    current_value: float,
    ath_value: float,
    ath_date: str,
    config: TacticalConfig,
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
    today = date.today().isoformat()

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
            # Returning to baseline: clear frozen reference
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
    current_index = REGIMES.index(current_regime)

    # Next deploy level is one deeper
    next_index = current_index + 1
    if next_index >= len(REGIMES):
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
        "target_regime": REGIMES[next_index],
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
            triggers.append({
                "drawdown_pct": threshold["drawdown_pct"],
                "fixed_pct": threshold["fixed_pct"],
                "dollar_value": dollar_value,
                "target_regime": target_regime or "baseline",
            })

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

    # Separate equity targets from fixed/cash targets
    equity_targets = {}
    cash_targets = {}
    for symbol, pct in targets.items():
        if symbol in fixed_symbols:
            # Will be overridden by tactical
            continue
        elif symbol in ("CAD", "USD"):
            cash_targets[symbol] = pct
        else:
            equity_targets[symbol] = pct

    # Calculate the current equity sum (what we're scaling from)
    current_equity_sum = sum(equity_targets.values())

    # The target equity sum based on posture (minus cash targets)
    cash_sum = sum(cash_targets.values())
    target_equity_sum = posture.equity_pct - cash_sum

    # Scale equity targets proportionally
    if current_equity_sum > 0 and target_equity_sum > 0:
        scale_factor = target_equity_sum / current_equity_sum
        equity_targets = {
            symbol: pct * scale_factor
            for symbol, pct in equity_targets.items()
        }

    # Compute fixed-income targets from composition ratios
    fixed_targets = {
        symbol: posture.fixed_pct * ratio
        for symbol, ratio in config.fixed_composition.items()
    }

    # Combine all targets
    result = {}
    result.update(cash_targets)
    result.update(equity_targets)
    result.update(fixed_targets)

    return result
