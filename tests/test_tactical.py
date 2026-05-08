"""Tests for the tactical defensive deployment module."""

import json
import tempfile
import os
from unittest.mock import patch

import pytest

from src.tactical import (
    TacticalConfig,
    TacticalState,
    TacticalPosture,
    parse_tactical_config,
    evaluate_tactical_posture,
    resolve_tactical_targets,
    load_tactical_state,
    save_tactical_state,
    _determine_regime,
    _fixed_pct_for_regime,
    _regime_for_fixed_pct,
)


# ══════════════════════════════════════════════════════════════════
# Test fixtures
# ══════════════════════════════════════════════════════════════════


@pytest.fixture
def sample_config():
    """Standard tactical config matching the example settings."""
    return TacticalConfig(
        baseline_equity_pct=80.0,
        fixed_composition={
            "ZMMK.TO": 0.50,
            "XSH.TO": 0.25,
            "XIGS.TO": 0.25,
        },
        deploy_thresholds=[
            {"drawdown_pct": -10.0, "fixed_pct": 15.0},
            {"drawdown_pct": -20.0, "fixed_pct": 10.0},
            {"drawdown_pct": -30.0, "fixed_pct": 5.0},
        ],
        recovery_thresholds=[
            {"drawdown_pct": -15.0, "fixed_pct": 10.0},
            {"drawdown_pct": -5.0, "fixed_pct": 15.0},
            {"drawdown_pct": 5.0, "fixed_pct": 20.0},
        ],
    )


@pytest.fixture
def temp_state_dir(tmp_path):
    """Provide a temporary directory for tactical state files."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    state_file = data_dir / "tactical_state.json"
    with patch("src.tactical.get_tactical_state_file", return_value=state_file):
        yield state_file


# ══════════════════════════════════════════════════════════════════
# Config parsing tests
# ══════════════════════════════════════════════════════════════════


class TestParseConfig:
    def test_returns_none_when_empty(self):
        result = parse_tactical_config({})
        assert result is None

    def test_returns_none_when_none(self):
        result = parse_tactical_config(None)
        assert result is None

    def test_parses_valid_config(self):
        raw = {
            "baseline_equity_pct": 80.0,
            "fixed_composition": {
                "ZMMK.TO": 50.0,
                "XSH.TO": 25.0,
                "XIGS.TO": 25.0,
            },
            "deploy_thresholds": [
                {"drawdown_pct": -10.0, "fixed_pct": 15.0},
                {"drawdown_pct": -20.0, "fixed_pct": 10.0},
                {"drawdown_pct": -30.0, "fixed_pct": 5.0},
            ],
            "recovery_thresholds": [
                {"drawdown_pct": -15.0, "fixed_pct": 10.0},
                {"drawdown_pct": -5.0, "fixed_pct": 15.0},
                {"drawdown_pct": 5.0, "fixed_pct": 20.0},
            ],
        }
        config = parse_tactical_config(raw)
        assert config is not None
        assert config.baseline_equity_pct == 80.0
        assert len(config.fixed_composition) == 3
        assert abs(sum(config.fixed_composition.values()) - 1.0) < 0.001

    def test_normalizes_ratios(self):
        raw = {
            "baseline_equity_pct": 80.0,
            "fixed_composition": {
                "ZMMK.TO": 50,
                "XSH.TO": 25,
                "XIGS.TO": 25,
            },
            "deploy_thresholds": [{"drawdown_pct": -10.0, "fixed_pct": 15.0}],
            "recovery_thresholds": [{"drawdown_pct": 5.0, "fixed_pct": 20.0}],
        }
        config = parse_tactical_config(raw)
        assert abs(config.fixed_composition["ZMMK.TO"] - 0.50) < 0.001

    def test_deploy_thresholds_sorted_descending(self):
        raw = {
            "baseline_equity_pct": 80.0,
            "fixed_composition": {"ZMMK.TO": 1.0},
            "deploy_thresholds": [
                {"drawdown_pct": -30.0, "fixed_pct": 5.0},
                {"drawdown_pct": -10.0, "fixed_pct": 15.0},
                {"drawdown_pct": -20.0, "fixed_pct": 10.0},
            ],
            "recovery_thresholds": [],
        }
        config = parse_tactical_config(raw)
        # Least negative first (descending = -10, -20, -30)
        assert config.deploy_thresholds[0]["drawdown_pct"] == -10.0
        assert config.deploy_thresholds[1]["drawdown_pct"] == -20.0
        assert config.deploy_thresholds[2]["drawdown_pct"] == -30.0


# ══════════════════════════════════════════════════════════════════
# Regime state machine tests
# ══════════════════════════════════════════════════════════════════


class TestDetermineRegime:
    def test_stays_at_baseline_no_drawdown(self, sample_config):
        result = _determine_regime("baseline", 0.0, sample_config)
        assert result == "baseline"

    def test_stays_at_baseline_small_drawdown(self, sample_config):
        result = _determine_regime("baseline", -5.0, sample_config)
        assert result == "baseline"

    def test_deploys_to_level_1_at_10pct(self, sample_config):
        result = _determine_regime("baseline", -10.0, sample_config)
        assert result == "level_1"

    def test_deploys_to_level_1_beyond_10pct(self, sample_config):
        result = _determine_regime("baseline", -12.0, sample_config)
        assert result == "level_1"

    def test_deploys_to_level_2_at_20pct_from_baseline(self, sample_config):
        """From baseline at -20%, jumps directly to level_2.
        The system deploys to the deepest qualifying level in one shot."""
        result = _determine_regime("baseline", -20.0, sample_config)
        assert result == "level_2"

    def test_deploys_to_level_3_at_30pct_from_baseline(self, sample_config):
        """From baseline at -30%, jumps directly to level_3.
        Flash crash triggers full deployment in a single evaluation."""
        result = _determine_regime("baseline", -30.0, sample_config)
        assert result == "level_3"

    def test_stays_at_level_1_during_drawdown(self, sample_config):
        """At -15%, level_1 should stay (not recover, not deploy further)."""
        result = _determine_regime("level_1", -15.0, sample_config)
        assert result == "level_1"

    def test_level_1_deploys_to_level_2(self, sample_config):
        result = _determine_regime("level_1", -20.0, sample_config)
        assert result == "level_2"

    def test_level_2_deploys_to_level_3(self, sample_config):
        result = _determine_regime("level_2", -30.0, sample_config)
        assert result == "level_3"

    def test_level_3_stays_at_max_deployment(self, sample_config):
        result = _determine_regime("level_3", -40.0, sample_config)
        assert result == "level_3"

    def test_recovery_level_2_to_level_1(self, sample_config):
        """At -5%, level_2 should recover to level_1."""
        result = _determine_regime("level_2", -5.0, sample_config)
        assert result == "level_1"

    def test_recovery_level_1_to_baseline(self, sample_config):
        """At +5%, level_1 should recover to baseline."""
        result = _determine_regime("level_1", 5.0, sample_config)
        assert result == "baseline"

    def test_no_recovery_at_boundary(self, sample_config):
        """At -6%, level_2 should NOT recover (threshold is -5%)."""
        result = _determine_regime("level_2", -6.0, sample_config)
        assert result == "level_2"

    def test_hysteresis_prevents_whipsaw(self, sample_config):
        """At -10% from level_1, should NOT go back to baseline (recovery is +5%)."""
        result = _determine_regime("level_1", -10.0, sample_config)
        assert result == "level_1"


class TestFixedPctForRegime:
    def test_baseline(self, sample_config):
        assert _fixed_pct_for_regime("baseline", sample_config) == 20.0

    def test_level_1(self, sample_config):
        assert _fixed_pct_for_regime("level_1", sample_config) == 15.0

    def test_level_2(self, sample_config):
        assert _fixed_pct_for_regime("level_2", sample_config) == 10.0

    def test_level_3(self, sample_config):
        assert _fixed_pct_for_regime("level_3", sample_config) == 5.0


class TestRegimeForFixedPct:
    def test_baseline_pct(self, sample_config):
        assert _regime_for_fixed_pct(20.0, sample_config) == "baseline"

    def test_level_1_pct(self, sample_config):
        assert _regime_for_fixed_pct(15.0, sample_config) == "level_1"

    def test_level_2_pct(self, sample_config):
        assert _regime_for_fixed_pct(10.0, sample_config) == "level_2"

    def test_level_3_pct(self, sample_config):
        assert _regime_for_fixed_pct(5.0, sample_config) == "level_3"

    def test_unknown_pct(self, sample_config):
        assert _regime_for_fixed_pct(12.0, sample_config) is None


# ══════════════════════════════════════════════════════════════════
# State persistence tests
# ══════════════════════════════════════════════════════════════════


class TestStatePersistence:
    def test_load_missing_file(self, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            state = load_tactical_state()
        assert state.regime == "baseline"
        assert state.reference_high is None

    def test_save_and_load(self, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            state = TacticalState(
                regime="level_1",
                reference_high=1000000.0,
                reference_high_date="2026-01-15",
                last_transition_date="2026-05-07",
            )
            save_tactical_state(state)

            loaded = load_tactical_state()
            assert loaded.regime == "level_1"
            assert loaded.reference_high == 1000000.0
            assert loaded.reference_high_date == "2026-01-15"
            assert loaded.last_transition_date == "2026-05-07"

    def test_save_baseline_omits_reference(self, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            state = TacticalState(regime="baseline")
            save_tactical_state(state)

            with open(state_file) as f:
                data = json.load(f)
            assert "reference_high" not in data
            assert "reference_high_date" not in data


# ══════════════════════════════════════════════════════════════════
# Full evaluation tests
# ══════════════════════════════════════════════════════════════════


class TestEvaluatePosture:
    def test_baseline_no_drawdown(self, sample_config, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            posture = evaluate_tactical_posture(
                current_value=1000000.0,
                ath_value=1000000.0,
                ath_date="2026-05-07",
                config=sample_config,
            )
        assert posture.regime == "baseline"
        assert posture.fixed_pct == 20.0
        assert posture.equity_pct == 80.0
        assert posture.transition_occurred is False

    def test_baseline_to_level_1(self, sample_config, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            posture = evaluate_tactical_posture(
                current_value=900000.0,  # -10% from ATH
                ath_value=1000000.0,
                ath_date="2026-01-15",
                config=sample_config,
            )
        assert posture.regime == "level_1"
        assert posture.fixed_pct == 15.0
        assert posture.equity_pct == 85.0
        assert posture.transition_occurred is True
        assert posture.previous_regime == "baseline"
        assert posture.reference_high == 1000000.0

    def test_freezes_reference_on_first_deploy(self, sample_config, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            # First transition
            evaluate_tactical_posture(
                current_value=900000.0,
                ath_value=1000000.0,
                ath_date="2026-01-15",
                config=sample_config,
            )

            # Verify state file was written with frozen reference
            with open(state_file) as f:
                data = json.load(f)
            assert data["regime"] == "level_1"
            assert data["reference_high"] == 1000000.0
            assert data["reference_high_date"] == "2026-01-15"

    def test_recovery_back_to_baseline(self, sample_config, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        # Pre-seed state at level_1
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({
                "regime": "level_1",
                "reference_high": 1000000.0,
                "reference_high_date": "2026-01-15",
                "last_transition_date": "2026-03-01",
            }, f)

        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            posture = evaluate_tactical_posture(
                current_value=1050000.0,  # +5% above reference
                ath_value=1050000.0,
                ath_date="2026-05-07",
                config=sample_config,
            )
        assert posture.regime == "baseline"
        assert posture.transition_occurred is True
        assert posture.previous_regime == "level_1"

    def test_trigger_info_at_level_1(self, sample_config, tmp_path):
        state_file = tmp_path / "data" / "tactical_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump({
                "regime": "level_1",
                "reference_high": 1000000.0,
                "reference_high_date": "2026-01-15",
            }, f)

        with patch("src.tactical.get_tactical_state_file", return_value=state_file):
            posture = evaluate_tactical_posture(
                current_value=880000.0,
                ath_value=1000000.0,
                ath_date="2026-01-15",
                config=sample_config,
            )

        # Should have a deploy trigger to level_2
        assert posture.next_deploy_trigger is not None
        assert posture.next_deploy_trigger["drawdown_pct"] == -20.0
        assert posture.next_deploy_trigger["dollar_value"] == 800000.0

        # Should have recovery triggers
        assert len(posture.next_recovery_triggers) > 0


# ══════════════════════════════════════════════════════════════════
# Target resolution tests
# ══════════════════════════════════════════════════════════════════


class TestResolveTargets:
    def test_baseline_preserves_equity(self, sample_config):
        targets = {
            "CAD": 0.0,
            "USD": 0.0,
            "VSP.TO": 53.0,
            "IVV": 21.0,
            "XEF.TO": 6.0,
        }
        posture = TacticalPosture(
            regime="baseline",
            fixed_pct=20.0,
            equity_pct=80.0,
            reference_high=1000000.0,
            reference_high_date="2026-01-15",
            drawdown_from_reference_pct=0.0,
        )
        result = resolve_tactical_targets(targets, posture, sample_config)

        # Fixed targets should be set
        assert "ZMMK.TO" in result
        assert "XSH.TO" in result
        assert "XIGS.TO" in result
        assert abs(result["ZMMK.TO"] - 10.0) < 0.01  # 20% * 0.50
        assert abs(result["XSH.TO"] - 5.0) < 0.01  # 20% * 0.25
        assert abs(result["XIGS.TO"] - 5.0) < 0.01  # 20% * 0.25

        # Total should be 100%
        total = sum(result.values())
        assert abs(total - 100.0) < 0.01

    def test_level_1_scales_equity_up(self, sample_config):
        targets = {
            "CAD": 0.0,
            "USD": 0.0,
            "VSP.TO": 53.0,
            "IVV": 21.0,
            "XEF.TO": 6.0,
        }
        posture = TacticalPosture(
            regime="level_1",
            fixed_pct=15.0,
            equity_pct=85.0,
            reference_high=1000000.0,
            reference_high_date="2026-01-15",
            drawdown_from_reference_pct=-12.0,
        )
        result = resolve_tactical_targets(targets, posture, sample_config)

        # Fixed is smaller
        assert abs(result["ZMMK.TO"] - 7.5) < 0.01  # 15% * 0.50
        assert abs(result["XSH.TO"] - 3.75) < 0.01  # 15% * 0.25

        # Equity should be scaled up proportionally
        # Original equity sum = 80%, new equity sum = 85%
        # Scale factor = 85/80 = 1.0625
        assert result["VSP.TO"] > 53.0  # Scaled up
        assert abs(result["VSP.TO"] - 53.0 * (85.0 / 80.0)) < 0.01

        # Total should be 100%
        total = sum(result.values())
        assert abs(total - 100.0) < 0.01

    def test_level_3_max_deployment(self, sample_config):
        targets = {
            "CAD": 0.0,
            "USD": 0.0,
            "VSP.TO": 53.0,
            "IVV": 21.0,
            "XEF.TO": 6.0,
        }
        posture = TacticalPosture(
            regime="level_3",
            fixed_pct=5.0,
            equity_pct=95.0,
            reference_high=1000000.0,
            reference_high_date="2026-01-15",
            drawdown_from_reference_pct=-32.0,
        )
        result = resolve_tactical_targets(targets, posture, sample_config)

        # Fixed is minimal
        assert abs(result["ZMMK.TO"] - 2.5) < 0.01  # 5% * 0.50
        assert abs(result["XSH.TO"] - 1.25) < 0.01  # 5% * 0.25
        assert abs(result["XIGS.TO"] - 1.25) < 0.01  # 5% * 0.25

        # Total should be 100%
        total = sum(result.values())
        assert abs(total - 100.0) < 0.01

    def test_preserves_cash_targets(self, sample_config):
        targets = {
            "CAD": 1.0,
            "USD": 0.5,
            "VSP.TO": 53.0,
            "IVV": 21.0,
            "XEF.TO": 4.5,
        }
        posture = TacticalPosture(
            regime="level_1",
            fixed_pct=15.0,
            equity_pct=85.0,
            reference_high=1000000.0,
            reference_high_date="2026-01-15",
            drawdown_from_reference_pct=-12.0,
        )
        result = resolve_tactical_targets(targets, posture, sample_config)

        # Cash targets preserved
        assert result["CAD"] == 1.0
        assert result["USD"] == 0.5
