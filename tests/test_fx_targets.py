"""Tests for the FX target resolution module."""

from unittest.mock import patch

import pytest

from src.fx_targets import resolve_targets, _clamp, _sticky_round


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


class TestStickyRound:
    def test_no_prior_rounds_to_nearest(self):
        assert _sticky_round(37.3, 2.0, None) == 38.0
        assert _sticky_round(36.9, 2.0, None) == 36.0

    def test_stays_sticky_within_one_step(self):
        # Current is 56, raw is 57.5 — hasn't reached 58 — stays at 56
        assert _sticky_round(57.5, 2.0, 56.0) == 56.0
        # Current is 56, raw is 54.5 — hasn't reached 54 — stays at 56
        assert _sticky_round(54.5, 2.0, 56.0) == 56.0

    def test_moves_up_at_boundary(self):
        # Current is 56, raw reaches 58 — moves to 58
        assert _sticky_round(58.0, 2.0, 56.0) == 58.0
        # Current is 56, raw exceeds 58 — still moves to 58 (not 60)
        assert _sticky_round(59.5, 2.0, 56.0) == 58.0

    def test_moves_down_at_boundary(self):
        # Current is 56, raw reaches 54 — moves to 54
        assert _sticky_round(54.0, 2.0, 56.0) == 54.0
        # Current is 56, raw below 54 but above 52 — still moves to 54
        assert _sticky_round(52.5, 2.0, 56.0) == 54.0

    def test_jumps_multiple_steps(self):
        # Current is 56, raw drops to 50 — jumps to 52 (3 steps down)
        assert _sticky_round(50.0, 2.0, 56.0) == 50.0
        # Current is 56, raw rises to 62 — jumps to 62
        assert _sticky_round(62.0, 2.0, 56.0) == 62.0

    def test_fractional_step(self):
        # Step = 2.5, current = 10.0
        assert _sticky_round(11.0, 2.5, 10.0) == 10.0  # Within one step
        assert _sticky_round(12.5, 2.5, 10.0) == 12.5  # Reached next step


@patch("src.fx_targets._save_fx_state")
@patch("src.fx_targets._load_fx_state", return_value={})
class TestResolveTargets:
    def test_no_fx_rules_returns_base_targets(self, mock_load, mock_save):
        base = {"VCN.TO": 30.0, "VUN.TO": 40.0, "XBB.TO": 30.0}
        result = resolve_targets(base, {}, 1.36)
        assert result == base

    def test_rule_adds_symbols(self, mock_load, mock_save):
        base = {"VCN.TO": 26.0, "CAD": 0.0, "USD": 0.0}
        rules = {
            "sp500_split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 1,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert "IVV" in result
        assert "VSP.TO" in result
        assert abs(result["IVV"] + result["VSP.TO"] - 74.0) < 1.0

    def test_midpoint_rate_splits_evenly(self, mock_load, mock_save):
        """At the midpoint of the range, allocation should be ~50/50."""
        base = {"XBB.TO": 26.0}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 1,
            }
        }
        midpoint_rate = 1.35  # midpoint of 1.20-1.50
        result = resolve_targets(base, rules, midpoint_rate)
        assert abs(result["VSP.TO"] - 37.0) < 1.0  # ~50% of 74
        assert abs(result["IVV"] - 37.0) < 1.0  # ~50% of 74

    def test_rate_at_max_favors_cad(self, mock_load, mock_save):
        """When USD is expensive (max rate), most goes to CAD fund."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 1,
            }
        }
        result = resolve_targets(base, rules, 1.50)
        assert result["VSP.TO"] == 74.0
        assert result["IVV"] == 0.0

    def test_rate_at_min_favors_usd(self, mock_load, mock_save):
        """When USD is cheap (min rate), most goes to USD fund."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 1,
            }
        }
        result = resolve_targets(base, rules, 1.20)
        assert result["VSP.TO"] == 0.0
        assert result["IVV"] == 74.0

    def test_rate_outside_range_clamps(self, mock_load, mock_save):
        """Rates beyond the range pin to one extreme."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 1,
            }
        }
        # Way above max
        result = resolve_targets(base, rules, 2.00)
        assert result["VSP.TO"] == 74.0

    def test_duplicate_symbol_in_targets_raises(self, mock_load, mock_save):
        """FX-managed symbols must not also appear in static targets."""
        base = {"VSP.TO": 50.0}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 1,
            }
        }
        with pytest.raises(ValueError, match="must not also appear in targets"):
            resolve_targets(base, rules, 1.36)

    def test_invalid_rate_range_raises(self, mock_load, mock_save):
        """max_rate must be greater than min_rate."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.50,
                "max_usd_to_cad_rate": 1.20,
                "target_rounding_step": 1,
            }
        }
        with pytest.raises(ValueError, match="greater than"):
            resolve_targets(base, rules, 1.36)

    def test_same_symbol_for_both_raises(self, mock_load, mock_save):
        base = {}
        rules = {
            "split": {
                "usd_symbol": "VSP.TO",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
            }
        }
        with pytest.raises(ValueError, match="different symbols"):
            resolve_targets(base, rules, 1.36)

    def test_rounding_step_one_gives_whole_numbers(self, mock_load, mock_save):
        """With step 1, targets are whole numbers."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_step": 1,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert result["VSP.TO"] == int(result["VSP.TO"])
        assert result["IVV"] == int(result["IVV"])

    def test_rounding_step_two(self, mock_load, mock_save):
        """With step 2, targets are multiples of 2."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 20.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_step": 2,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert result["VSP.TO"] % 2 == 0
        assert result["IVV"] % 2 == 0

    def test_rounding_step_two_point_five(self, mock_load, mock_save):
        """With step 2.5, targets are multiples of 2.5."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 20.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_step": 2.5,
            }
        }
        result = resolve_targets(base, rules, 1.25)
        # 50% -> cad=10, usd=10 both multiples of 2.5
        assert result["VSP.TO"] % 2.5 == 0
        assert result["IVV"] % 2.5 == 0

    def test_rounding_step_fractional(self, mock_load, mock_save):
        """With step 0.01, targets have fine-grained precision."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 0.01,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        # With 0.01 step, should be precise to two decimals
        assert abs(result["VSP.TO"] - round(result["VSP.TO"], 2)) < 1e-9
        assert abs(result["IVV"] - round(result["IVV"], 2)) < 1e-9
        # Should still sum close to total
        assert abs(result["IVV"] + result["VSP.TO"] - 74.0) < 0.02

    def test_rounding_step_zero_raises(self, mock_load, mock_save):
        """Step must be > 0."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": 0,
            }
        }
        with pytest.raises(ValueError, match="target_rounding_step must be > 0"):
            resolve_targets(base, rules, 1.36)

    def test_rounding_step_negative_raises(self, mock_load, mock_save):
        """Step must be > 0."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.20,
                "max_usd_to_cad_rate": 1.50,
                "target_rounding_step": -1,
            }
        }
        with pytest.raises(ValueError, match="target_rounding_step must be > 0"):
            resolve_targets(base, rules, 1.36)

    def test_default_step_is_one(self, mock_load, mock_save):
        """When target_rounding_step is omitted, default is 1 (whole numbers)."""
        base = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
            }
        }
        result = resolve_targets(base, rules, 1.36)
        assert result["VSP.TO"] == int(result["VSP.TO"])
        assert result["IVV"] == int(result["IVV"])


@patch("src.fx_targets._save_fx_state")
@patch("src.fx_targets._load_fx_state")
class TestStickyBehavior:
    def test_stays_sticky_when_rate_fluctuates(self, mock_load, mock_save):
        """Target stays unchanged when raw value hasn't reached a full step."""
        mock_load.return_value = {"split": {"cad_target_pct": 56.0}}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_step": 2,
            }
        }
        # Rate that produces raw ~55.8 (just below 56, within one step)
        # cad_fraction = (1.378 - 1.0) / 0.5 = 0.756 → raw = 74 * 0.756 = 55.94
        result = resolve_targets({}, rules, 1.378)
        assert result["VSP.TO"] == 56.0  # Stays sticky

    def test_moves_when_rate_reaches_next_step(self, mock_load, mock_save):
        """Target changes when raw calculation reaches a full step boundary."""
        mock_load.return_value = {"split": {"cad_target_pct": 56.0}}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_step": 2,
            }
        }
        # Rate that produces raw >= 58: cad_fraction = 58/74 = 0.7838 → rate = 1.0 + 0.7838*0.5 = 1.3919
        result = resolve_targets({}, rules, 1.393)
        assert result["VSP.TO"] == 58.0  # Moved up

    def test_persists_new_state(self, mock_load, mock_save):
        """State is saved after resolution."""
        mock_load.return_value = {}
        rules = {
            "split": {
                "usd_symbol": "IVV",
                "cad_symbol": "VSP.TO",
                "total_target_pct": 74.0,
                "min_usd_to_cad_rate": 1.0,
                "max_usd_to_cad_rate": 1.5,
                "target_rounding_step": 2,
            }
        }
        resolve_targets({}, rules, 1.36)
        mock_save.assert_called_once()
        saved_state = mock_save.call_args[0][0]
        assert "split" in saved_state
        assert "cad_target_pct" in saved_state["split"]
