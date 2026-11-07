"""Shared configuration, export loading and valuation for external CAD accounts.

Settings provide labels and Questrade routes; registered adapters supply source
data. load_external_balances refreshes encrypted caches and movement evidence,
then attach_external_balances adds reported balances to the household portfolio.
Transfer reconciliation applies transit/offset adjustments separately.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from src.encrypted_state import read_encrypted_json, write_encrypted_json
from src.external_providers import PROVIDERS, provider_for
from src.models import ExternalExportNotFound
from src.paths import get_data_dir
from src.portfolio import ExternalBalance, HoldingSummary


@dataclass(frozen=True)
class ExternalAccountConfig:
    """Full provider-qualified identity, console labels and native Questrade route."""

    symbol: str
    display_name: str
    account_type: str
    questrade_owner: str
    questrade_type: str

    @property
    def identifier(self) -> str:
        return self.symbol.split(":", 1)[1]


def parse_external_accounts(settings: dict) -> dict[str, ExternalAccountConfig]:
    """Target membership enables accounts; each enabled account needs explicit labels/routing."""
    static = settings.get("targets", {})
    tactical = settings.get("tactical_deployment", {})
    raw = settings.get("external_accounts", {})
    if (
        not isinstance(static, dict)
        or not isinstance(tactical, dict)
        or not isinstance(raw, dict)
    ):
        raise ValueError(
            "targets, tactical_deployment and external_accounts must be mappings."
        )
    fixed = tactical.get("fixed_composition", {})
    if not isinstance(fixed, dict):
        raise ValueError("fixed_composition must be a mapping.")
    configured = {}
    for symbol, account in raw.items():
        provider_for(symbol)
        if not isinstance(account, dict) or set(account) != {
            "display_name",
            "account_type",
            "questrade_account",
        }:
            raise ValueError(
                "External accounts require display_name, account_type and questrade_account."
            )
        route = account["questrade_account"]
        if not isinstance(route, dict) or set(route) != {"owner_name", "account_type"}:
            raise ValueError("questrade_account requires owner_name and account_type.")
        for value in (
            account["display_name"],
            account["account_type"],
            route["owner_name"],
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    "External display names, account types and Questrade owners must be nonempty strings."
                )
        if route["account_type"] != "Margin":
            raise ValueError(
                "External transfers require a personal Questrade Margin account."
            )
        configured[symbol] = ExternalAccountConfig(
            symbol,
            account["display_name"].strip(),
            account["account_type"].strip(),
            route["owner_name"].strip(),
            route["account_type"],
        )
    enabled = {}
    # Target membership, including zero weight, enables tracking. Metadata can
    # exist without enabling an account, but enabled accounts need explicit labels.
    for mapping in (static, fixed):
        for symbol, weight in mapping.items():
            if not isinstance(symbol, str) or ":" not in symbol:
                continue
            provider_for(symbol)
            try:
                valid = math.isfinite(float(weight)) and float(weight) >= 0
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError(
                    "External target weights must be finite nonnegative numbers."
                )
            if symbol in enabled:
                raise ValueError(
                    "An external account must appear in only one target mapping."
                )
            if fixed and symbol in static:
                raise ValueError(
                    "With tactical deployment, external accounts belong in fixed_composition."
                )
            if symbol in settings.get("transient_symbols", []):
                raise ValueError("External balances cannot be transient symbols.")
            if symbol not in configured:
                raise ValueError(
                    "Every external target requires an external_accounts settings entry."
                )
            enabled[symbol] = configured[symbol]
    for rule in settings.get("fx_target_rules", {}).values():
        if any(":" in str(rule.get(key, "")) for key in ("usd_symbol", "cad_symbol")):
            raise ValueError("External accounts cannot be FX target instruments.")
    return enabled


def resolve_external_transfer_accounts(portfolio, accounts: dict) -> dict:
    """Resolve configured owner/type pairs to unique personal Questrade accounts."""
    endpoints = {}
    for symbol, config in accounts.items():
        matches = [
            account
            for account in portfolio.accounts
            if account.owner == config.questrade_owner
            and account.account_type == config.questrade_type
            and account.client_account_type == "Individual"
        ]
        if len(matches) != 1:
            raise ValueError(
                f"External transfers require exactly one {config.questrade_owner} {config.questrade_type} account."
            )
        endpoints[symbol] = matches[0]
    return endpoints


def validate_external_balance(identifier: str, balance: dict) -> None:
    """Require the common four-field cache shape, CAD value and dated source identity."""
    if not isinstance(balance, dict) or set(balance) != {
        "balance_cad",
        "last_transaction_date",
        "source_modified_at",
        "source_sha256",
    }:
        raise ValueError(
            f"Invalid cached external balance fields for account ending {identifier[-4:]}."
        )
    try:
        value = Decimal(balance["balance_cad"])
        if not value.is_finite() or value < 0 or not math.isfinite(float(value)):
            raise ValueError("Invalid balance.")
        date.fromisoformat(balance["last_transaction_date"])
        if datetime.fromisoformat(balance["source_modified_at"]).utcoffset() is None:
            raise ValueError("Source times must include a timezone.")
        digest = balance["source_sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("Invalid source identity.")
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise ValueError(
            f"Invalid cached external balance/source data for account ending {identifier[-4:]}."
        ) from None


def refresh_external_balances(
    provider,
    identifiers,
    existing,
    directory=None,
    source_file=None,
    on_status=None,
    on_export=None,
):
    """Select current snapshots without writing state; return (snapshot, refreshed IDs).

    Without a source, retain the cached balance and its dates. Source hashes detect
    repeats; both activity date and file time protect against replacing newer data.
    Callbacks expose status and the matching ledger evidence needed when saving.
    """
    if directory is not None and source_file is not None:
        raise ValueError("Specify a directory or a source file, not both.")
    if existing and "accounts" not in existing:
        raise ValueError("The external snapshot must contain an accounts mapping.")
    cached = existing.get("accounts", {})
    if not isinstance(cached, dict):
        raise ValueError("The external snapshot accounts must be a mapping.")
    snapshots, refreshed = {}, set()
    for identifier in identifiers:
        prior = cached.get(identifier)
        if prior is not None:
            validate_external_balance(identifier, prior)
        exported = None
        if source_file is not None:
            exported = provider.read_export(source_file, identifier)
        elif directory is not None:
            try:
                path = provider.find_export(directory, identifier)
            except ExternalExportNotFound:
                path = None
            if path is not None:
                exported = provider.read_export(path, identifier)
        if exported is None:
            if prior is None:
                raise ValueError(
                    f"No export or cached balance for configured account ending {identifier[-4:]}."
                )
            snapshots[identifier] = prior
            status = (
                "absent"
                if directory is not None or source_file is not None
                else "cached"
            )
        elif prior is not None and exported.source_sha256 == prior["source_sha256"]:
            snapshots[identifier], status = prior, "unchanged"
        elif prior is not None and (
            prior["last_transaction_date"] > exported.last_transaction_date.isoformat()
            or datetime.fromisoformat(prior["source_modified_at"])
            > exported.source_modified_at
        ):
            snapshots[identifier], status = prior, "older"
        else:
            snapshots[identifier] = exported.as_snapshot()
            validate_external_balance(identifier, snapshots[identifier])
            refreshed.add(identifier)
            status = "found"
        if on_status is not None:
            on_status(
                f"{provider.key}:{identifier}",
                status,
                snapshots[identifier]["last_transaction_date"],
            )
        if (
            on_export is not None
            and exported is not None
            and exported.source_sha256 == snapshots[identifier]["source_sha256"]
        ):
            on_export(identifier, exported)
    return {"accounts": snapshots}, refreshed


def save_external_balances(
    provider, destination, snapshot, existing, exports, active_symbols
) -> bool:
    """Reconcile export evidence before saving the cache; return whether it changed."""
    from src.transfer_tracking import TransferLedger

    transfer_path = destination.parent / "transfer_tracking.json"
    if transfer_path.exists() or exports:
        ledger = TransferLedger.load(transfer_path)
        # This refresh handles one provider; retain routes for every active provider.
        ledger.retain_routes(active_symbols)
        for identifier, balance in snapshot["accounts"].items():
            ledger.observe_external_balance(
                f"{provider.key}:{identifier}", balance, exports.get(identifier)
            )
        ledger.save()
    # Persist the observation cursor first. A retry can safely replay this export;
    # saving only its balance first would lose the evidence of its cash movement.
    if snapshot != existing:
        write_encrypted_json(destination, snapshot)
        return True
    return False


def load_external_balances(accounts: dict, directory=None, on_status=None) -> dict:
    """Refresh per-provider caches and return balances keyed by full external symbol.

    directory=None uses saved data only, as scheduled runs require. Loading can
    write cache/transfer state when a source changes; it is not a read-only query.
    """
    balances = {}
    groups = {}
    for symbol, config in accounts.items():
        provider = provider_for(symbol)
        groups.setdefault(provider.key, []).append(config.identifier)
    for key, identifiers in groups.items():
        provider = PROVIDERS[key]
        destination = get_data_dir() / provider.cache_filename
        existing = read_encrypted_json(destination) if destination.exists() else {}
        exports = {}
        snapshot, _ = refresh_external_balances(
            provider,
            identifiers,
            existing,
            directory,
            on_status=on_status,
            on_export=lambda identifier, exported: exports.update(
                {identifier: exported}
            ),
        )
        save_external_balances(
            provider, destination, snapshot, existing, exports, set(accounts)
        )
        balances.update(
            {
                f"{key}:{identifier}": balance
                for identifier, balance in snapshot["accounts"].items()
            }
        )
    return balances


def attach_external_balances(portfolio, balances: dict, accounts: dict) -> None:
    """Add reported external values and labels once, without adding broker cash."""
    for symbol, balance in balances.items():
        validate_external_balance(accounts[symbol].identifier, balance)
        if symbol in portfolio.holdings:
            raise ValueError("External balance is already included in the portfolio.")
    for symbol, balance in balances.items():
        config = accounts[symbol]
        value = float(balance["balance_cad"])
        portfolio.holdings[symbol] = HoldingSummary(value_cad=value)
        portfolio.external_balances[symbol] = ExternalBalance(
            value,
            balance["last_transaction_date"],
            balance["source_modified_at"],
            config.display_name,
            config.account_type,
        )
        portfolio.total_value_cad += value
