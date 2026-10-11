"""Config-owned Saven account selection and cached export balances."""

from pathlib import Path
import re

from src.saven_export import (
    SavenExportNotFound,
    find_latest_saven_export,
    read_saven_export,
)


def parse_saven_account_numbers(settings: dict) -> list[str]:
    section = settings.get("saven", {})
    if not isinstance(section, dict):
        raise ValueError("saven must be a configuration mapping.")
    numbers = section.get("account_numbers", [])
    if not isinstance(numbers, list) or any(
        not isinstance(number, str) or not re.fullmatch(r"\d+", number)
        for number in numbers
    ):
        raise ValueError("saven.account_numbers must be a list of quoted digit strings.")
    if len(set(numbers)) != len(numbers):
        raise ValueError("saven.account_numbers contains duplicate accounts.")
    return numbers


def refresh_saven_balances(
    account_numbers: list[str],
    existing: dict,
    directory: Path | None = None,
    source_file: Path | None = None,
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
        if cached is not None and (
            not isinstance(cached, dict) or cached.get("account_number") != number
        ):
            raise ValueError("A cached Saven balance has a mismatched account identity.")
        if cached is not None and any(
            key not in cached for key in (
                "balance_cad", "last_transaction_date", "source_modified_at",
                "source_filename", "source_sha256", "imported_at",
            )
        ):
            raise ValueError("A cached Saven balance is missing required source data.")
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
        if exported is None or (
            cached is not None and exported.source_sha256 == cached.get("source_sha256")
        ):
            if cached is None:
                raise ValueError(f"No export or cached balance for configured account ending {number[-4:]}.")
            snapshots[number] = cached
            continue
        if cached is not None and (
            cached.get("last_transaction_date", "") > exported.last_transaction_date.isoformat()
            or cached.get("source_modified_at", "") > exported.source_modified_at.isoformat()
        ):
            snapshots[number] = cached
            continue
        snapshots[number] = exported.as_snapshot()
        refreshed.add(number)
    return {"accounts": snapshots}, refreshed
