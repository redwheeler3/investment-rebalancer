"""Tests for scheduled-sync failure handling."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import main
from src.encrypted_state import EncryptedStateError


def _config():
    return (
        [
            {
                "owner_name": "Primary",
                "token_file": "primary.json",
                "account_type_display_overrides": {},
            }
        ],
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
        pytest.raises(EncryptedStateError, match="safe encrypted-state failure"),
    ):
        main.run_scheduled_sync()


def test_scheduled_sync_keeps_market_snapshot_best_effort(capsys):
    with (
        patch("main.load_config", return_value=_config()),
        patch("main.validate_encrypted_state_files"),
        patch("main.get_tokens_dir"),
        patch("main.QuestradeClient", return_value=object()),
        patch(
            "main.get_usd_to_cad_rate", side_effect=RuntimeError("market unavailable")
        ),
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
        pytest.raises(EncryptedStateError, match="plaintext state rejected"),
    ):
        main.run_scheduled_sync()

    client.assert_not_called()


def test_interactive_connection_fails_closed_when_one_account_fails(tmp_path, capsys):
    accounts = [
        {
            "owner_name": "Jeff",
            "token_file": "jeff.json",
            "account_type_display_overrides": {},
        },
        {
            "owner_name": "Eunee",
            "token_file": "eunee.json",
            "account_type_display_overrides": {},
        },
    ]
    for filename in ("jeff.json", "eunee.json"):
        (tmp_path / filename).write_text("encrypted placeholder")

    with (
        patch("main.get_tokens_dir", return_value=Path(tmp_path)),
        patch(
            "main.QuestradeClient",
            side_effect=[object(), RuntimeError("temporary failure")],
        ) as client,
        pytest.raises(SystemExit) as exc_info,
    ):
        main._connect_clients(accounts)

    assert exc_info.value.code == 1
    assert client.call_count == 2
    output = capsys.readouterr().out
    assert "Jeff connected" in output
    assert "Eunee connection failed" in output
    assert "requires every configured Questrade account" in output
    assert "No portfolio state was evaluated" in output


def test_partial_connection_stops_before_portfolio_or_tactical_evaluation(tmp_path):
    accounts = [
        {
            "owner_name": "Jeff",
            "token_file": "jeff.json",
            "account_type_display_overrides": {},
        },
        {
            "owner_name": "Eunee",
            "token_file": "eunee.json",
            "account_type_display_overrides": {},
        },
    ]
    for filename in ("jeff.json", "eunee.json"):
        (tmp_path / filename).write_text("encrypted placeholder")

    config = (accounts, {}, [], 0.0, {}, 1.0, object())
    with (
        patch("main.load_config", return_value=config),
        patch("main.get_tokens_dir", return_value=Path(tmp_path)),
        patch(
            "main.QuestradeClient",
            side_effect=[object(), RuntimeError("temporary failure")],
        ),
        patch("main._fetch_exchange_rate") as fetch_rate,
        patch("main._build_priced_portfolio") as build_portfolio,
        patch("main.evaluate_tactical_posture") as evaluate_tactical,
        pytest.raises(SystemExit),
    ):
        main.run_rebalancer()

    fetch_rate.assert_not_called()
    build_portfolio.assert_not_called()
    evaluate_tactical.assert_not_called()


def test_interactive_connection_fails_closed_when_token_file_is_missing(
    tmp_path, capsys
):
    accounts = [
        {
            "owner_name": "Jeff",
            "token_file": "missing.json",
            "account_type_display_overrides": {},
        }
    ]

    with (
        patch("main.get_tokens_dir", return_value=Path(tmp_path)),
        patch("main.QuestradeClient") as client,
        pytest.raises(SystemExit) as exc_info,
    ):
        main._connect_clients(accounts)

    assert exc_info.value.code == 1
    client.assert_not_called()
    output = capsys.readouterr().out
    assert "token file not found" in output
    assert "No portfolio state was evaluated" in output
