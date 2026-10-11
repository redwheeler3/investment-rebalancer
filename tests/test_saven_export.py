"""Saven export parsing, ledger reconciliation, and account-safe file selection."""

from datetime import date
from decimal import Decimal
import os

import pytest

from src.saven_export import find_latest_saven_export, read_saven_export


def write_export(path, rows, account="0001112223"):
    path.write_text(
        f"Your Transactions List\n\nAccount Number:  {account}\n\n"
        "Date,Description,Amount (CAD), Balance (CAD) \n"
        + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


def test_reads_bank_balance_with_unquoted_commas_and_unsigned_withdrawal(tmp_path):
    path = write_export(tmp_path / "random-guid.csv", [
        "07/10/2026,Withdrawal,100.00, +$1,150.00 ",
        "30/09/2026,Interest,50.00, +$1,250.00 ",
        "01/09/2026,Deposit,1200.00, +$1,200.00 ",
    ])
    result = read_saven_export(path)
    assert result.balance_cad == Decimal("1150.00")
    assert result.last_transaction_date == date(2026, 10, 7)
    assert result.transaction_count == 3
    assert result.as_snapshot()["balance_cad"] == "1150.00"
    assert "imported_at" in result.as_snapshot()


def test_preserves_newest_first_order_for_transactions_on_same_day(tmp_path):
    path = write_export(tmp_path / "same-day.csv", [
        '07/10/2026,"Transfer, incoming",10.00, +$20.00 ',
        "07/10/2026,Deposit,10.00, +$10.00 ",
    ])
    assert read_saven_export(path).balance_cad == Decimal("20.00")


@pytest.mark.parametrize("rows, message", [
    ([], "no transactions"),
    (["07/10/2026,Deposit,10.00,"], "Invalid transaction"),
    (["07/10/2026,Deposit,10.00, +$1,00.00 "], "Invalid transaction"),
    (["07/10/2026,Deposit,1,000.00, +$1,000.00 "], "Invalid transaction"),
    (["07/10/2026,Interest,10.00, +$20.00 ",
      "01/10/2026,Deposit,5.00, +$5.00 "], "do not reconcile"),
    (["01/10/2026,Deposit,5.00, +$5.00 ",
      "07/10/2026,Interest,10.00, +$15.00 "], "newest-first"),
])
def test_rejects_missing_ambiguous_or_inconsistent_data(tmp_path, rows, message):
    path = write_export(tmp_path / "invalid.csv", rows)
    with pytest.raises(ValueError, match=message):
        read_saven_export(path)


def test_directory_selection_uses_format_and_time_not_filename(tmp_path):
    old = write_export(tmp_path / "z-old-guid.csv", ["01/10/2026,Deposit,10.00, +$10.00 "])
    new = write_export(tmp_path / "a-new-guid.csv", ["07/10/2026,Deposit,20.00, +$20.00 "])
    unrelated = tmp_path / "unrelated.csv"
    unrelated.write_text("Date,Description,Amount\n", encoding="utf-8")
    os.utime(old, (100, 100))
    os.utime(new, (200, 200))
    os.utime(unrelated, (300, 300))
    assert find_latest_saven_export(tmp_path) == new


def test_directory_selection_requires_account_when_multiple_accounts_exist(tmp_path):
    first = write_export(tmp_path / "one.csv", ["01/10/2026,Deposit,10.00, +$10.00 "])
    write_export(tmp_path / "two.csv", ["01/10/2026,Deposit,20.00, +$20.00 "], account="0001114445")
    with pytest.raises(ValueError, match="multiple accounts"):
        find_latest_saven_export(tmp_path)
    assert find_latest_saven_export(tmp_path, "0001112223") == first


def test_directory_selection_rejects_tied_file_times(tmp_path):
    first = write_export(tmp_path / "one.csv", ["01/10/2026,Deposit,10.00, +$10.00 "])
    second = write_export(tmp_path / "two.csv", ["01/10/2026,Deposit,20.00, +$20.00 "])
    for path in (first, second):
        os.utime(path, (100, 100))
    with pytest.raises(ValueError, match="newest file time"):
        find_latest_saven_export(tmp_path)
