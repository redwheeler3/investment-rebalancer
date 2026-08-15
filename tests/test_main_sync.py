"""Tests for scheduled-sync failure handling."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from src.encrypted_state import EncryptedStateError


def _config():
    return (
        [{
            "owner_name": "Primary",
            "token_file": "primary.json",
            "account_type_display_overrides": {},
        }],
        {},
        [],
        0.0,
        {},
        0.1,
        None,
    )


def test_scheduled_sync_fails_on_encrypted_state_error():
    portfolio = SimpleNamespace(total_value_cad=100.0)
    with (
        patch("main.load_config", return_value=_config()),
        patch("main.validate_encrypted_state_files"),
        patch("main.get_tokens_dir"),
        patch("main.QuestradeClient", return_value=object()),
        patch("main.get_usd_to_cad_rate", return_value=1.35),
        patch("main.build_portfolio", return_value=portfolio),
        patch(
            "main.record_value",
            side_effect=EncryptedStateError("safe encrypted-state failure"),
        ),
    ):
        with pytest.raises(EncryptedStateError, match="safe encrypted-state failure"):
            main.run_scheduled_sync()


def test_scheduled_sync_keeps_market_snapshot_best_effort(capsys):
    with (
        patch("main.load_config", return_value=_config()),
        patch("main.validate_encrypted_state_files"),
        patch("main.get_tokens_dir"),
        patch("main.QuestradeClient", return_value=object()),
        patch("main.get_usd_to_cad_rate", side_effect=RuntimeError("market unavailable")),
    ):
        main.run_scheduled_sync()

    output = capsys.readouterr().out
    assert "Could not snapshot portfolio value: market unavailable" in output
    assert "Portfolio sync complete." in output


def test_scheduled_sync_validates_before_rotating_tokens():
    with (
        patch("main.load_config", return_value=_config()),
        patch(
            "main.validate_encrypted_state_files",
            side_effect=EncryptedStateError("plaintext state rejected"),
        ),
        patch("main.QuestradeClient") as client,
    ):
        with pytest.raises(EncryptedStateError, match="plaintext state rejected"):
            main.run_scheduled_sync()

    client.assert_not_called()
