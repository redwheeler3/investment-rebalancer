"""Tests for the FX target resolution module."""

import pytest

from src.fx_targets import resolve_targets, _clamp


class TestClamp:
    def test_value_within_range(self):
        assert _clamp(1.35, 1.20, 1.50) == 1.35

    def test_value_below_minimum(self):
        assert _clamp(1.10, 1.20, 1.50) == 1.20

    def test_value_above_maximum(self):
        assert _clamp(1.60, 1.20, 1.50) == 1.50

    def test_value_at_minimum(self):
        assert _clamp(1.20, 1.20, 1.50) == 1.20

    def test_value_at_maximum(self):
        assert _clamp(1.50, 1.20, 1.50) == 1.50


class TestResolveTargets:
    def test_no_fx_rules_returns_base_targets(self):
        base = {"VCN.TO": 30.0, "VUN.TO": 40.0, "XBB.TO": 30.0}
        result = resolve_targets(base, {}, 1.36)
        assert result == base

    def test_disabled_rule_ignored(self):
        base = {"VCN.TO": 30.0, "XBB.TO": 30.0}
        rules = {
            "test_split": {
                "enabled": False,
                "usd_symbol": "IVV",
                "cad_symbol": "XSP.TO",
                "total_target_pct": 40.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert "IVV" not in result
        assert "XSP.TO" not in result

    def test_enabled_rule_adds_symbols(self):
        base = {"VCN.TO": 26.0, "CAD": 0.0, "USD": 0.0}
        rules = {
            "sp500_split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert "IVV" in result
        assert "VSP.TO" in result
        assert abs(result["IVV"] + result["VSP.TO"] - 74.0) < 0.01

    def test_midpoint_rate_splits_evenly(self):
        """At the midpoint of the range, allocation should be ~50/50."""
        base = {"XBB.TO": 26.0}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        midpoint_rate = 1.35  # midpoint of 1.20-1.50
        result = resolve_targets(base, rules, midpoint_rate)
        assert abs(result["VSP.TO"] - 37.0) < 0.5  # ~50% of 74
        assert abs(result["IVV"] - 37.0) < 0.5  # ~50% of 74

    def test_rate_at_max_favors_cad(self):
        """When USD is expensive (max rate), most goes to CAD fund."""
        base = {}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        result = resolve_targets(base, rules, 1.50)
        assert result["VSP.TO"] == 74.0
        assert result["IVV"] == 0.0

    def test_rate_at_min_favors_usd(self):
        """When USD is cheap (min rate), most goes to USD fund."""
        base = {}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        result = resolve_targets(base, rules, 1.20)
        assert result["VSP.TO"] == 0.0
        assert result["IVV"] == 74.0

    def test_rate_outside_range_clamps(self):
        """Rates beyond the range pin to one extreme."""
        base = {}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        # Way above max
        result = resolve_targets(base, rules, 2.00)
        assert result["VSP.TO"] == 74.0

    def test_duplicate_symbol_in_targets_raises(self):
        """FX-managed symbols must not also appear in static targets."""
        base = {"VSP.TO": 50.0}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_decimals": 2,
            }
        }
        with pytest.raises(ValueError, match="must not also appear in targets"):
            resolve_targets(base, rules, 1.36)

    def test_invalid_rate_range_raises(self):
        """max_rate must be greater than min_rate."""
        base = {}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.50,
                "max_usd_to_cad_rate": 1.20,
                "target_rounding_decimals": 2,
            }
        }
        with pytest.raises(ValueError, match="greater than"):
            resolve_targets(base, rules, 1.36)

    def test_same_symbol_for_both_raises(self):
        base = {}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "VSP.TO",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
            }
        }
        with pytest.raises(ValueError, match="different symbols"):
            resolve_targets(base, rules, 1.36)

    def test_rounding_decimals_zero(self):
        """With 0 decimals, targets are whole numbers."""
        base = {}
        rules = {
            "split": {
                "enabled": True,
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_decimals": 0,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert result["VSP.TO"] == int(result["VSP.TO"])
        assert result["IVV"] == int(result["IVV"])
