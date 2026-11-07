"""Register external statement adapters; Questrade remains the native broker.

Adding an institution means providing ID validation, discovery, a normalized
reader and a cache filename here. Shared valuation/planning use their common
output; console names and account types come from settings.
"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from src.models import ExternalExport
from src.saven_export import (
    find_latest_saven_export,
    read_saven_export,
    validate_saven_identifier,
)


@dataclass(frozen=True)
class ExternalProvider:
    """Adapter registration; user-facing names belong to account settings."""

    key: str
    cache_filename: str
    validate_identifier: Callable[[str], None]
    find_export: Callable[[Path, str | None], Path]
    read_export: Callable[[Path, str | None], ExternalExport | None]


PROVIDERS = {
    "SAVEN": ExternalProvider(
        "SAVEN",
        "saven_balance.json",
        validate_saven_identifier,
        find_latest_saven_export,
        read_saven_export,
    ),
}


def provider_for(symbol: str) -> ExternalProvider:
    """Validate PROVIDER:<account-id> and select its registered statement adapter."""
    if not isinstance(symbol, str) or ":" not in symbol:
        raise ValueError("External account identifiers must use PROVIDER:<account-id>.")
    key, identifier = symbol.split(":", 1)
    if key not in PROVIDERS:
        raise ValueError(f"Unsupported external account provider: {key}.")
    if not identifier or identifier != identifier.strip():
        raise ValueError(
            "External account identifiers must contain an account ID without surrounding whitespace."
        )
    provider = PROVIDERS[key]
    provider.validate_identifier(identifier)
    return provider
