"""FX-based target allocation rules.

Dynamically splits target allocations between CAD/USD fund pairs
based on the current exchange rate (configured via fx_target_rules in settings.yaml).
"""


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


def _resolve_fx_split_rule(rule_name: str, rule: dict, targets: dict, usd_to_cad_rate: float) -> None:
    """Apply one FX split rule to the target mapping in place."""
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
    cad_target_pct = round(total_target_pct * cad_fraction / rounding_step) * rounding_step
    usd_target_pct = round((total_target_pct - cad_target_pct) / rounding_step) * rounding_step

    targets[usd_symbol] = usd_target_pct
    targets[cad_symbol] = cad_target_pct


def resolve_targets(base_targets: dict, fx_target_rules: dict, usd_to_cad_rate: float) -> dict:
    """Resolve a final flat target map from static targets and FX-based rules."""
    targets = dict(base_targets)

    for rule_name, rule in fx_target_rules.items():
        _resolve_fx_split_rule(rule_name, rule, targets, usd_to_cad_rate)

    return targets
