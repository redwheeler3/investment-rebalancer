"""FX-based target allocation rules.

Dynamically splits target allocations between CAD/USD fund pairs
based on the current exchange rate (configured via fx_target_rules in settings.yaml).

Sticky rounding: when a target_rounding_step is configured, the resolved target
only changes when the raw (unrounded) calculation reaches a full step away from
the current value. This prevents oscillation when the rate hovers near a
rounding boundary.
"""

import json

from src.paths import get_fx_targets_state_file


def _clamp(value: float, minimum: float, maximum: float) -> float:
    """Clamp a number to an inclusive range."""
    return max(minimum, min(maximum, value))


def _parse_float(rule_name: str, field_name: str, value) -> float:
    """Parse a config value as float, raising a clear error on failure."""
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"fx_target_rules.{rule_name}.{field_name} must be a number"
        ) from exc


def _sticky_round(raw_value: float, step: float, current_value: float | None) -> float:
    """Round raw_value to the nearest step, sticky toward current_value.

    If current_value is known and raw_value hasn't reached a full step away,
    keep the current value. Otherwise round to nearest.
    """
    nearest = round(raw_value / step) * step
    if current_value is None:
        return nearest

    # Only change if the raw value has reached the next step boundary
    if raw_value >= current_value + step:
        # Moved up — snap to the step at or below raw_value
        return current_value + step * int((raw_value - current_value) / step)
    elif raw_value <= current_value - step:
        # Moved down — snap to the step at or above raw_value
        return current_value - step * int((current_value - raw_value) / step)
    else:
        # Still within one step of current — stay sticky
        return current_value


# ══════════════════════════════════════════════════════════════════
# State persistence
# ══════════════════════════════════════════════════════════════════


def _load_fx_state() -> dict:
    """Load persisted FX target state, or return empty dict."""
    state_file = get_fx_targets_state_file()
    if not state_file.exists():
        return {}
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_fx_state(state: dict) -> None:
    """Persist FX target state to disk."""
    state_file = get_fx_targets_state_file()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


# ══════════════════════════════════════════════════════════════════
# Rule resolution
# ══════════════════════════════════════════════════════════════════


def _resolve_fx_split_rule(
    rule_name: str,
    rule: dict,
    targets: dict,
    usd_to_cad_rate: float,
    prior_cad_target: float | None,
) -> float:
    """Apply one FX split rule to the target mapping in place.

    Returns the resolved cad_target_pct (for state persistence).
    """
    usd_symbol = rule.get("usd_symbol")
    cad_symbol = rule.get("cad_symbol")
    if not usd_symbol or not cad_symbol:
        raise ValueError(
            f"fx_target_rules.{rule_name} must define both usd_symbol and cad_symbol"
        )
    if usd_symbol == cad_symbol:
        raise ValueError(
            f"fx_target_rules.{rule_name} must use different symbols for usd_symbol and cad_symbol"
        )

    if usd_symbol in targets or cad_symbol in targets:
        raise ValueError(
            f"fx_target_rules.{rule_name} manages {usd_symbol}/{cad_symbol}, so those symbols must not also appear in targets"
        )

    total_target_pct = _parse_float(rule_name, "total_target_pct", rule.get("total_target_pct"))
    min_rate = _parse_float(rule_name, "min_usd_to_cad_rate", rule.get("min_usd_to_cad_rate"))
    max_rate = _parse_float(rule_name, "max_usd_to_cad_rate", rule.get("max_usd_to_cad_rate"))
    rounding_step = _parse_float(rule_name, "target_rounding_step", rule.get("target_rounding_step", 1))

    if total_target_pct < 0:
        raise ValueError(f"fx_target_rules.{rule_name}.total_target_pct must be >= 0")
    if rounding_step <= 0:
        raise ValueError(
            f"fx_target_rules.{rule_name}.target_rounding_step must be > 0"
        )
    if max_rate <= min_rate:
        raise ValueError(
            f"fx_target_rules.{rule_name} must have max_usd_to_cad_rate greater than min_usd_to_cad_rate"
        )

    clamped_rate = _clamp(usd_to_cad_rate, min_rate, max_rate)
    cad_fraction = (clamped_rate - min_rate) / (max_rate - min_rate)
    raw_cad_pct = total_target_pct * cad_fraction

    cad_target_pct = _sticky_round(raw_cad_pct, rounding_step, prior_cad_target)
    usd_target_pct = round((total_target_pct - cad_target_pct) / rounding_step) * rounding_step

    targets[usd_symbol] = usd_target_pct
    targets[cad_symbol] = cad_target_pct
    return cad_target_pct


def resolve_targets(base_targets: dict, fx_target_rules: dict, usd_to_cad_rate: float) -> dict:
    """Resolve a final flat target map from static targets and FX-based rules."""
    targets = dict(base_targets)

    if not fx_target_rules:
        return targets

    state = _load_fx_state()
    new_state = {}

    for rule_name, rule in fx_target_rules.items():
        prior_cad_target = state.get(rule_name, {}).get("cad_target_pct")
        cad_target_pct = _resolve_fx_split_rule(
            rule_name, rule, targets, usd_to_cad_rate, prior_cad_target,
        )
        new_state[rule_name] = {"cad_target_pct": cad_target_pct}

    _save_fx_state(new_state)
    return targets
