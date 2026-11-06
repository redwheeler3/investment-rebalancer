"""Config-owned Saven account selection and cached export balances."""

import math
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.encrypted_state import read_encrypted_json, write_encrypted_json
from src.paths import get_data_dir
from src.portfolio import ExternalBalance, HoldingSummary
from src.saven_export import (
    SavenExportBalance,
    SavenExportNotFound,
    find_latest_saven_export,
    read_saven_export,
)


def parse_saven_account_numbers(settings: dict) -> list[str]:
    """Select external accounts from allocation membership, including zero targets."""
    static = settings.get("targets", {})
    tactical = settings.get("tactical_deployment", {})
    if not isinstance(static, dict) or not isinstance(tactical, dict):
        raise ValueError("targets and tactical_deployment must be mappings.")
    fixed = tactical.get("fixed_composition", {})
    if not isinstance(fixed, dict):
        raise ValueError("fixed_composition must be a mapping.")
    numbers = []
    for mapping in (static, fixed):
        for symbol, weight in mapping.items():
            if not isinstance(symbol, str) or not symbol.upper().startswith("SAVEN"):
                continue
            match = re.fullmatch(r"SAVEN:(\d+)", symbol)
            if not match:
                raise ValueError(
                    "Saven targets must use quoted SAVEN:<account-number> identifiers."
                )
            try:
                valid_weight = math.isfinite(float(weight)) and float(weight) >= 0
            except (TypeError, ValueError):
                valid_weight = False
            if not valid_weight:
                raise ValueError(
                    "Saven target weights must be finite nonnegative numbers."
                )
            number = match.group(1)
            if number in numbers:
                raise ValueError(
                    "A Saven account must appear in only one target mapping."
                )
            if fixed and symbol in static:
                raise ValueError(
                    "With tactical deployment, Saven belongs in fixed_composition."
                )
            if symbol in settings.get("transient_symbols", []):
                raise ValueError("Saven balances cannot be transient symbols.")
            numbers.append(number)
    for rule in settings.get("fx_target_rules", {}).values():
        if any(
            str(rule.get(key, "")).upper().startswith("SAVEN")
            for key in ("usd_symbol", "cad_symbol")
        ):
            raise ValueError("Saven balances cannot be FX target instruments.")
    return numbers


def parse_external_transfer_accounts(
    settings: dict, account_numbers: list[str]
) -> dict:
    """Configure the personal Margin account used by each external holding."""
    routes = settings.get("external_transfer_accounts", {})
    if not isinstance(routes, dict):
        raise ValueError("external_transfer_accounts must be a mapping.")
    for symbol, route in routes.items():
        if not re.fullmatch(r"SAVEN:\d+", str(symbol)) or not isinstance(route, dict):
            raise ValueError(
                "Each external transfer account requires a SAVEN identifier and account mapping."
            )
        if (
            not isinstance(route.get("owner_name"), str)
            or not route["owner_name"].strip()
        ):
            raise ValueError("An external transfer account requires owner_name.")
        if route.get("account_type") != "Margin":
            raise ValueError("External transfers require a personal Margin account.")
    if any(f"SAVEN:{number}" not in routes for number in account_numbers):
        raise ValueError(
            "Every Saven target requires an external_transfer_accounts entry."
        )
    return routes


def resolve_external_transfer_accounts(portfolio, routes: dict) -> dict:
    """Require one explicit owner/type match; never choose a registered account."""
    accounts = {}
    for symbol in portfolio.external_balances:
        route = routes[symbol]
        matches = [
            account
            for account in portfolio.accounts
            if account.owner == route["owner_name"]
            and account.account_type == route["account_type"]
            and account.client_account_type == "Individual"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"External transfers require exactly one {route['owner_name']} {route['account_type']} account."
            )
        accounts[symbol] = matches[0]
    return accounts


def validate_saven_balance(number: str, balance: dict) -> None:
    """Reject corrupted cached financial data before it reaches portfolio math."""
    if not isinstance(balance, dict) or set(balance) != {
        "balance_cad",
        "last_transaction_date",
        "source_modified_at",
        "source_sha256",
    }:
        raise ValueError(
            f"Invalid cached Saven balance fields for account ending {number[-4:]}."
        )
    try:
        value = Decimal(balance["balance_cad"])
        if not value.is_finite() or value < 0 or not math.isfinite(float(value)):
            raise ValueError("Invalid balance.")
        date.fromisoformat(balance["last_transaction_date"])
        if datetime.fromisoformat(balance["source_modified_at"]).utcoffset() is None:
            raise ValueError("Source times must include a timezone.")
        if not re.fullmatch(r"[a-f0-9]{64}", balance["source_sha256"]):
            raise ValueError("Invalid source identity.")
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise ValueError(
            f"Invalid cached Saven balance/source data for account ending {number[-4:]}."
        ) from None


def refresh_saven_balances(
    account_numbers: list[str],
    existing: dict,
    directory: Path | None = None,
    source_file: Path | None = None,
    on_status: Callable[[str, str, str], None] | None = None,
    on_export: Callable[[str, SavenExportBalance], None] | None = None,
) -> tuple[dict, set[str]]:
    """Refresh configured accounts; retain their dated snapshots without new exports."""
    if directory is not None and source_file is not None:
        raise ValueError("Specify a directory or a source file, not both.")
    if existing and "accounts" not in existing:
        raise ValueError("The Saven snapshot must contain an accounts mapping.")
    cached_accounts = existing.get("accounts", {})
    if not isinstance(cached_accounts, dict):
        raise ValueError("The Saven snapshot accounts must be a mapping.")
    snapshots = {}
    refreshed = set()
    for number in account_numbers:
        cached = cached_accounts.get(number)
        if cached is not None:
            validate_saven_balance(number, cached)
        exported = None
        if source_file is not None:
            exported = read_saven_export(source_file, number)
        elif directory is not None:
            try:
                path = find_latest_saven_export(directory, number)
            except SavenExportNotFound:
                path = None
            if path is not None:
                exported = read_saven_export(path, number)
        if exported is None:
            if cached is None:
                raise ValueError(
                    f"No export or cached balance for configured account ending {number[-4:]}."
                )
            snapshots[number] = cached
            status = (
                "absent"
                if directory is not None or source_file is not None
                else "cached"
            )
        elif cached is not None and exported.source_sha256 == cached["source_sha256"]:
            snapshots[number] = cached
            status = "unchanged"
        elif cached is not None and (
            cached.get("last_transaction_date", "")
            > exported.last_transaction_date.isoformat()
            or datetime.fromisoformat(cached["source_modified_at"])
            > exported.source_modified_at
        ):
            snapshots[number] = cached
            status = "older"
        else:
            snapshots[number] = exported.as_snapshot()
            validate_saven_balance(number, snapshots[number])
            refreshed.add(number)
            status = "found"
        if on_status is not None:
            on_status(number, status, snapshots[number]["last_transaction_date"])
        if (
            on_export is not None
            and exported is not None
            and exported.source_sha256 == snapshots[number]["source_sha256"]
        ):
            on_export(number, exported)
    return {"accounts": snapshots}, refreshed


def load_saven_balances(
    account_numbers: list[str],
    directory: Path | None = None,
    on_status: Callable[[str, str, str], None] | None = None,
) -> dict:
    """Local runs refresh exports; scheduled runs consume encrypted snapshots only."""
    if not account_numbers:
        return {}
    destination = get_data_dir() / "saven_balance.json"
    existing = read_encrypted_json(destination) if destination.exists() else {}
    exports = {}
    snapshot, _ = refresh_saven_balances(
        account_numbers,
        existing,
        directory=directory,
        on_status=on_status,
        on_export=lambda number, exported: exports.update({number: exported}),
    )
    save_saven_balances(destination, snapshot, existing, exports)
    return snapshot["accounts"]


def save_saven_balances(
    destination: Path, snapshot: dict, existing: dict, exports: dict
) -> bool:
    """Update existing transfer evidence before publishing a changed bank snapshot."""
    from src.transfer_tracking import TransferLedger

    transfer_path = destination.parent / "transfer_tracking.json"
    if transfer_path.exists() or exports:
        ledger = TransferLedger.load(transfer_path)
        ledger.retain_routes({f"SAVEN:{number}" for number in snapshot["accounts"]})
        for number, balance in snapshot["accounts"].items():
            ledger.observe_saven(f"SAVEN:{number}", balance, exports.get(number))
        ledger.save()
    if snapshot != existing:
        write_encrypted_json(destination, snapshot)
        return True
    return False


def attach_saven_balances(portfolio, balances: dict) -> None:
    """Include dated savings in household valuation without creating broker cash."""
    for number, balance in balances.items():
        validate_saven_balance(number, balance)
        if f"SAVEN:{number}" in portfolio.holdings:
            raise ValueError("Saven balance is already included in the portfolio.")
    for number, balance in balances.items():
        symbol = f"SAVEN:{number}"
        value = float(balance["balance_cad"])
        portfolio.holdings[symbol] = HoldingSummary(value_cad=value)
        portfolio.external_balances[symbol] = ExternalBalance(
            balance_cad=value,
            last_transaction_date=balance["last_transaction_date"],
            source_modified_at=balance["source_modified_at"],
        )
        portfolio.total_value_cad += value
