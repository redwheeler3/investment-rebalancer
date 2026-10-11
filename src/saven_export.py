"""Read ledger balances from Saven's newest-first CAD transaction export."""

import csv
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
import hashlib
from pathlib import Path
import re


EXPORT_TITLE = "Your Transactions List"
EXPORT_HEADER = ["Date", "Description", "Amount (CAD)", "Balance (CAD)"]
NUMBER_PATTERN = r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}"


class SavenExportNotFound(ValueError):
    """No export is available for the requested account."""


@dataclass(frozen=True)
class SavenExportBalance:
    account_number: str
    balance_cad: Decimal
    last_transaction_date: date
    transaction_count: int
    source_filename: str
    source_sha256: str
    source_modified_at: datetime

    def as_snapshot(self) -> dict:
        """Describe source age separately from the time the importer runs."""
        return {
            "provider": "Saven Financial",
            "account_number": self.account_number,
            "currency": "CAD",
            "balance_cad": str(self.balance_cad),
            "last_transaction_date": self.last_transaction_date.isoformat(),
            "transaction_count": self.transaction_count,
            "source_filename": self.source_filename,
            "source_sha256": self.source_sha256,
            "source_modified_at": self.source_modified_at.isoformat(),
            "imported_at": datetime.now(UTC).isoformat(),
        }


def _read_prefix(reader, account_number: str | None = None) -> str | None:
    title = next(reader, [])
    if title != [EXPORT_TITLE]:
        return None
    separator = next(reader, [])
    account_row = next(reader, [])
    if len(account_row) != 1:
        raise ValueError("Unexpected Saven export preamble.")
    match = re.fullmatch(r"Account Number:\s*(\d+)\s*", account_row[0].strip())
    if not match:
        raise ValueError("Expected an account number in the Saven export.")
    exported_account = match.group(1)
    if account_number is not None and exported_account != account_number:
        return None
    header_separator = next(reader, [])
    header = next(reader, [])
    if separator or header_separator or [cell.strip() for cell in header] != EXPORT_HEADER:
        raise ValueError("Expected an account number and the Saven CAD export header.")
    return exported_account


def _read_cad(value: str, dollar_prefix: bool) -> Decimal:
    value = value.strip()
    prefix = r"[+-]?\$" if dollar_prefix else r"[+-]?"
    if not re.fullmatch(prefix + NUMBER_PATTERN, value):
        raise ValueError("Expected a CAD amount with two decimal places.")
    return Decimal(value.replace("$", "").replace(",", ""))


def read_saven_export(path: Path, account_number: str | None = None) -> SavenExportBalance | None:
    raw = path.read_bytes()
    reader = csv.reader(raw.decode("utf-8-sig").splitlines())
    exported_account = _read_prefix(reader, account_number)
    if exported_account is None:
        if account_number is not None:
            return None
        raise ValueError("This file does not have the Saven transaction-export format.")

    transactions = []
    for row_number, row in enumerate(reader, start=6):
        if not row or all(not cell.strip() for cell in row):
            continue
        try:
            if len(row) < 4:
                raise ValueError("Expected four transaction fields.")
            transaction_date = datetime.strptime(row[0].strip(), "%d/%m/%Y").date()
            amount = _read_cad(row[2], dollar_prefix=False)
            # The final currency field contains unquoted thousands separators.
            balance = _read_cad(
                ",".join(row[3:]), dollar_prefix=True,
            )
        except ValueError as error:
            raise ValueError(f"Invalid transaction at row {row_number}: {error}") from None
        transactions.append((transaction_date, amount, balance))

    if not transactions:
        raise ValueError("The export has no transactions with a reported balance.")
    for newer, older in zip(transactions, transactions[1:]):
        if newer[0] < older[0]:
            raise ValueError("Expected transactions in newest-first order.")
        # Amounts are unsigned in this export. Balances establish movement direction.
        if abs(newer[2] - older[2]) != abs(newer[1]):
            raise ValueError("Transaction amounts do not reconcile adjacent balances.")

    return SavenExportBalance(
        account_number=exported_account,
        balance_cad=transactions[0][2],
        last_transaction_date=transactions[0][0],
        transaction_count=len(transactions),
        source_filename=path.name,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
    )


def find_latest_saven_export(directory: Path, account_number: str | None = None) -> Path:
    """Select by export format/account and file time, independently of filename."""
    candidates = []
    for path in directory.glob("*.csv"):
        try:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                exported_account = _read_prefix(csv.reader(stream), account_number)
        except UnicodeDecodeError:
            continue
        if exported_account is not None:
            candidates.append((path, exported_account, path.stat().st_mtime_ns))
    if not candidates:
        raise SavenExportNotFound("No matching Saven-format CSV exports were found.")
    if len({account for _, account, _ in candidates}) != 1:
        raise ValueError("Exports cover multiple accounts. Specify --account-number or --file.")
    latest_time = max(modified for _, _, modified in candidates)
    newest = [path for path, _, modified in candidates if modified == latest_time]
    if len(newest) != 1:
        raise ValueError("Multiple exports have the newest file time. Specify --file.")
    return newest[0]
