"""A new statement adapter and settings work without changing the Questrade core."""

import hashlib
import importlib.util
import re
from datetime import UTC, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from src import display
from src.external_accounts import (
    attach_external_balances,
    load_external_balances,
    parse_external_accounts,
    resolve_external_transfer_accounts,
)
from src.external_providers import PROVIDERS, ExternalProvider
from src.models import (
    ExternalExport,
    ExternalExportNotFound,
    ExternalTransaction,
    TransferRecommendation,
)
from src.portfolio import (
    AccountInfo,
    HoldingSummary,
    PortfolioSummary,
    Position,
    build_allocation_snapshot,
)
from src.rebalancer import calculate_trades
from src.transfer_tracking import TransferLedger
from tests.test_transfer_tracking import NOW, activity

SYMBOL = "ACME:AB-77"


def validate_identifier(identifier):
    if re.fullmatch(r"[A-Z0-9-]+", identifier) is None:
        raise ValueError("Invalid example account ID.")


def read_example(path, identifier=None):
    """Deliberately not a Saven CSV; flags come from explicit activity codes."""
    raw = path.read_bytes()
    lines = raw.decode("utf-8").splitlines()
    if not lines or not lines[0].startswith("ACME ACCOUNT="):
        return None
    number = lines[0].split("=", 1)[1]
    if identifier is not None and number != identifier:
        return None
    rows = []
    for line in lines[1:]:
        day, kind, memo, amount, balance = line.split("|")
        rows.append(
            ExternalTransaction(
                datetime.fromisoformat(day).date(),
                memo,
                Decimal(amount),
                Decimal(balance),
                kind == "TRANSFER",
            )
        )
    return ExternalExport(
        number,
        rows[0].balance_cad,
        rows[0].date,
        hashlib.sha256(raw).hexdigest(),
        datetime.fromtimestamp(path.stat().st_mtime, UTC),
        tuple(rows),
    )


def find_example(directory, identifier=None):
    for path in directory.glob("*.psv"):
        if read_example(path, identifier) is not None:
            return path
    raise ExternalExportNotFound("No matching example statement.")


@pytest.fixture
def provider(monkeypatch):
    adapter = ExternalProvider(
        "ACME", "acme_balance.json", validate_identifier, find_example, read_example
    )
    monkeypatch.setitem(PROVIDERS, "ACME", adapter)
    return adapter


def settings():
    return {
        "targets": {"EQ": 85, SYMBOL: 15},
        "external_accounts": {
            SYMBOL: {
                "display_name": "Rainy Day",
                "account_type": "Reserve",
                "questrade_account": {
                    "owner_name": "Example",
                    "account_type": "Margin",
                },
            }
        },
    }


def statement(tmp_path, receipt=None):
    path = tmp_path / "example.psv"
    rows = ["2026-11-01|TRANSFER|Opening|100000.00|100000.00"]
    if receipt is not None:
        # "interest" in a memo does not override this adapter's transfer code.
        rows.insert(
            0,
            f"2026-11-02|TRANSFER|High interest reserve transfer|{receipt:.2f}|{100000 + receipt:.2f}",
        )
    path.write_text("ACME ACCOUNT=AB-77\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def portfolio(balances, configs, invested):
    account = AccountInfo("broker", "Margin", "Individual", "Example")
    account.positions = [
        Position(
            "EQ", 1, invested / 100, invested, 100, "CAD", "broker", "Margin", "Example"
        )
    ]
    result = PortfolioSummary(
        accounts=[account],
        holdings={
            "EQ": HoldingSummary(
                value_cad=invested,
                total_quantity=invested / 100,
                current_price=100,
                bid_price=100,
                ask_price=100,
            )
        },
        total_value_cad=invested,
    )
    attach_external_balances(result, balances, configs)
    return result


def test_new_parser_and_settings_drive_loading_tracking_planning_and_nomenclature(
    tmp_path, monkeypatch, provider, encrypted_store
):
    statement(tmp_path)
    configs = parse_external_accounts(settings())
    balances = load_external_balances(configs, tmp_path)
    initial = portfolio(balances, configs, 900000)
    endpoints = resolve_external_transfer_accounts(initial, configs)
    ledger = TransferLedger.load()
    ledger.observe_broker(SYMBOL, "broker", [], NOW)
    ledger.remember_recommendations(
        [
            TransferRecommendation(
                SYMBOL, "DEPOSIT", 50000, "broker", "Margin", "Example"
            )
        ],
        endpoints,
        NOW,
    )
    ledger.observe_broker(SYMBOL, "broker", [activity(-50000)])
    ledger.save()
    current = portfolio(balances, configs, 850000)
    ledger.apply(current, endpoints)
    assert current.total_value_cad == 1000000
    assert current.cash_cad_total == current.accounts[0].cash_cad == 0
    assert (
        build_allocation_snapshot(current, settings()["targets"], 1.3).accuracy == 100
    )
    actions = calculate_trades(
        current, settings()["targets"], 1.3, 0, 0.1, set(), None, endpoints
    )
    assert (
        len(actions) == 1 and actions[0].in_progress and actions[0].amount_cad == 50000
    )
    output = StringIO()
    monkeypatch.setattr(
        display, "console", Console(file=output, width=160, force_terminal=False)
    )
    display.display_holdings_summary(current, 1.3)
    display.display_account_summary(current, 1.3)
    display.display_allocations(
        build_allocation_snapshot(current, settings()["targets"], 1.3).allocations,
        settings()["targets"],
        0.1,
        current.external_balances,
    )
    display.display_external_transfers(current, actions)
    rendered = output.getvalue()
    assert "Rainy Day Reserve" in rendered and "Rainy Day Transfers" in rendered
    assert "ACME:...B-77" in rendered and SYMBOL not in rendered
    assert "Saven" not in rendered and "HISA" not in rendered
    assert "In transit" in rendered

    statement(tmp_path, receipt=75000)
    received = load_external_balances(configs, tmp_path)
    updated = TransferLedger.load()
    settled = portfolio(received, configs, 850000)
    updated.apply(settled, endpoints)
    assert settled.total_value_cad == 1025000
    assert settled.transfer_progress[0].in_transit_cad == 0
    data, storage = encrypted_store
    assert data / "acme_balance.json" in storage
    assert data / "saven_balance.json" not in storage


def test_display_labels_are_explicit_settings_not_provider_defaults(provider):
    raw = settings()
    del raw["external_accounts"][SYMBOL]["display_name"]
    with pytest.raises(ValueError, match="display_name"):
        parse_external_accounts(raw)


def test_unknown_provider_does_not_silently_become_a_questrade_symbol():
    with pytest.raises(ValueError, match="Unsupported external account provider"):
        parse_external_accounts({"targets": {"UNREGISTERED:123": 5}})


def test_configured_but_untargeted_accounts_do_not_enter_valuation(provider):
    raw = settings()
    raw["targets"] = {"EQ": 100}
    assert parse_external_accounts(raw) == {}


def test_shared_import_command_uses_a_new_adapter_and_configured_labels(
    tmp_path, monkeypatch, provider, encrypted_store, capsys
):
    import yaml

    statement(tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_text(
        yaml.safe_dump(settings()), encoding="utf-8"
    )
    path = (
        Path(__file__).resolve().parents[1] / "scripts" / "import_external_balance.py"
    )
    spec = importlib.util.spec_from_file_location("external_import_boundary", path)
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    monkeypatch.setattr(importer, "get_config_dir", lambda: config_dir)
    monkeypatch.setattr(importer, "get_data_dir", lambda: encrypted_store[0])
    monkeypatch.setattr(
        "sys.argv",
        [
            "import_external_balance",
            "--directory",
            str(tmp_path),
            "--account",
            SYMBOL,
            "--save",
        ],
    )
    assert importer.main() == 0
    assert encrypted_store[0] / "acme_balance.json" in encrypted_store[1]
    output = capsys.readouterr().out
    assert "Rainy Day Reserve" in output and "Saven" not in output


def test_refreshing_one_provider_does_not_drop_another_providers_pending_state(
    tmp_path, provider, encrypted_store
):
    from tests.test_saven_tracking import export as saven_statement

    statement(tmp_path)
    saven_statement(tmp_path / "saven.csv", account="0001112223", amount="100000.00")
    legacy = "SAVEN:0001112223"
    raw = settings()
    raw["targets"] = {"EQ": 80, SYMBOL: 15, legacy: 5}
    raw["external_accounts"][legacy] = {
        "display_name": "Existing",
        "account_type": "Savings",
        "questrade_account": {"owner_name": "Other", "account_type": "Margin"},
    }
    configs = parse_external_accounts(raw)
    load_external_balances(configs, tmp_path)
    ledger = TransferLedger.load()
    old_endpoint = AccountInfo("old-broker", "Margin", "Individual", "Other")
    endpoint = AccountInfo("broker", "Margin", "Individual", "Example")
    routes = {legacy: old_endpoint, SYMBOL: endpoint}
    ledger.observe_broker(legacy, "old-broker", [], NOW)
    ledger.observe_broker(SYMBOL, "broker", [], NOW)
    ledger.remember_recommendations(
        [
            TransferRecommendation(
                legacy, "DEPOSIT", 50000, "old-broker", "Margin", "Other"
            )
        ],
        routes,
        NOW,
    )
    ledger.observe_broker(legacy, "old-broker", [activity(-50000)])
    ledger.save()
    load_external_balances(dict(reversed(list(configs.items()))), tmp_path)
    reloaded = TransferLedger.load()
    assert set(reloaded.data["accounts"]) == {legacy, SYMBOL}
    assert reloaded.progress(legacy, old_endpoint).in_transit_cad == 50000
