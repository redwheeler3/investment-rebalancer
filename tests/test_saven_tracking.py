"""Configuration-owned account filtering and preservation of cached balance dates."""

import os

import pytest

from src.saven_export import read_saven_export
from src.saven_tracking import parse_saven_account_numbers, refresh_saven_balances


ACCOUNT_A = "0001112223"
ACCOUNT_B = "0001114445"


def export(path, account=ACCOUNT_A, amount="10.00", transaction_date="07/10/2026"):
    path.write_text(
        f"Your Transactions List\n\nAccount Number: {account}\n\n"
        "Date,Description,Amount (CAD), Balance (CAD) \n"
        f"{transaction_date},Deposit,{amount}, +${amount} \n",
        encoding="utf-8",
    )
    return path


def cached(path):
    result = read_saven_export(path)
    return {"accounts": {result.account_number: result.as_snapshot()}}


def test_config_preserves_leading_zeros_and_missing_section_disables_tracking():
    assert parse_saven_account_numbers({"saven": {"account_numbers": [ACCOUNT_A]}}) == [ACCOUNT_A]
    assert parse_saven_account_numbers({"targets": {"CAD": 0.0}}) == []


@pytest.mark.parametrize("section", [
    None,
    {"account_numbers": ACCOUNT_A},
    {"account_numbers": [1112223]},
    {"account_numbers": [""]},
    {"account_numbers": [" 0001112223"]},
    {"account_numbers": [ACCOUNT_A, ACCOUNT_A]},
])
def test_config_rejects_invalid_or_duplicate_account_identifiers(section):
    with pytest.raises(ValueError):
        parse_saven_account_numbers({"saven": section})


def test_newer_untracked_account_does_not_replace_tracked_account(tmp_path):
    tracked = export(tmp_path / "older-guid.csv")
    untracked = export(tmp_path / "newer-guid.csv", ACCOUNT_B, "20.00")
    os.utime(tracked, (100, 100))
    os.utime(untracked, (200, 200))
    snapshots, refreshed = refresh_saven_balances([ACCOUNT_A], {}, directory=tmp_path)
    assert set(snapshots["accounts"]) == {ACCOUNT_A}
    assert snapshots["accounts"][ACCOUNT_A]["balance_cad"] == "10.00"
    assert refreshed == {ACCOUNT_A}


def test_untracked_account_is_ignored_before_header_or_transaction_validation(tmp_path):
    export(tmp_path / "tracked.csv")
    (tmp_path / "untracked.csv").write_text(
        f"Your Transactions List\n\nAccount Number: {ACCOUNT_B}\nmalformed header and data\n",
        encoding="utf-8",
    )
    snapshots, _ = refresh_saven_balances([ACCOUNT_A], {}, directory=tmp_path)
    assert set(snapshots["accounts"]) == {ACCOUNT_A}


def test_newer_untracked_file_keeps_existing_tracked_balance_and_dates(tmp_path):
    initial = export(tmp_path / "initial.csv")
    existing = cached(initial)
    initial.unlink()
    export(tmp_path / "another-account.csv", ACCOUNT_B, "20.00")
    snapshots, refreshed = refresh_saven_balances([ACCOUNT_A], existing, directory=tmp_path)
    assert snapshots == existing
    assert not refreshed


def test_explicit_untracked_file_does_not_change_cached_account(tmp_path):
    existing = cached(export(tmp_path / "initial.csv"))
    other = export(tmp_path / "other.csv", ACCOUNT_B, "20.00")
    snapshots, refreshed = refresh_saven_balances([ACCOUNT_A], existing, source_file=other)
    assert snapshots == existing
    assert not refreshed


def test_multiple_configured_accounts_have_independent_balances(tmp_path):
    export(tmp_path / "one.csv", ACCOUNT_A, "10.00")
    export(tmp_path / "two.csv", ACCOUNT_B, "20.00")
    snapshots, refreshed = refresh_saven_balances([ACCOUNT_A, ACCOUNT_B], {}, directory=tmp_path)
    assert snapshots["accounts"][ACCOUNT_A]["balance_cad"] == "10.00"
    assert snapshots["accounts"][ACCOUNT_B]["balance_cad"] == "20.00"
    assert refreshed == {ACCOUNT_A, ACCOUNT_B}


def test_unchanged_export_does_not_advance_import_or_source_dates(tmp_path):
    path = export(tmp_path / "one.csv")
    existing = cached(path)
    snapshots, refreshed = refresh_saven_balances([ACCOUNT_A], existing, directory=tmp_path)
    assert snapshots == existing
    assert not refreshed


def test_removing_account_from_config_excludes_its_cache(tmp_path):
    existing = cached(export(tmp_path / "one.csv"))
    snapshots, refreshed = refresh_saven_balances([], existing)
    assert snapshots == {"accounts": {}}
    assert not refreshed


def test_missing_initial_balance_is_not_treated_as_zero(tmp_path):
    with pytest.raises(ValueError, match="No export or cached balance"):
        refresh_saven_balances([ACCOUNT_A], {}, directory=tmp_path)


def test_invalid_tracked_export_is_reported_even_if_cached_balance_exists(tmp_path):
    existing = cached(export(tmp_path / "one.csv"))
    invalid = export(tmp_path / "invalid.csv", amount="NaN")
    with pytest.raises(ValueError, match="Invalid transaction"):
        refresh_saven_balances([ACCOUNT_A], existing, source_file=invalid)


def test_older_export_retains_cached_balance(tmp_path):
    existing = cached(export(tmp_path / "one.csv"))
    older = export(tmp_path / "older.csv", amount="5.00", transaction_date="01/09/2026")
    snapshots, refreshed = refresh_saven_balances([ACCOUNT_A], existing, source_file=older)
    assert snapshots == existing
    assert not refreshed
