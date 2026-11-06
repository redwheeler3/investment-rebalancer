"""Read ledger balances from Saven's newest-first CAD transaction export."""

import csv
import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from src.models import ExternalExport, ExternalExportNotFound, ExternalTransaction

EXPORT_TITLE = "Your Transactions List"
EXPORT_HEADER = ["Date", "Description", "Amount (CAD)", "Balance (CAD)"]
NUMBER_PATTERN = r"(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}"


class SavenExportNotFound(ExternalExportNotFound):
    """No export is available for the requested account."""


def validate_saven_identifier(identifier: str) -> None:
    if not re.fullmatch(r"\d+", identifier):
        raise ValueError(
            "Saven identifiers must use SAVEN:<account-number> with digits."
        )


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
    normalized_header = [cell.strip() for cell in header]
    # Saven also exports the ledger column with only its currency label.
    valid_header = (
        normalized_header[:3] == EXPORT_HEADER[:3]
        and len(normalized_header) == 4
        and normalized_header[3] in {"Balance (CAD)", "(CAD)"}
    )
    if separator or header_separator or not valid_header:
        raise ValueError("Expected an account number and the Saven CAD export header.")
    return exported_account


def _read_cad(value: str, dollar_prefix: bool) -> Decimal:
    value = value.strip()
    prefix = r"[+-]?\$" if dollar_prefix else r"[+-]?"
    if not re.fullmatch(prefix + NUMBER_PATTERN, value):
        raise ValueError("Expected a CAD amount with two decimal places.")
    return Decimal(value.replace("$", "").replace(",", ""))


def read_saven_export(
    path: Path, account_number: str | None = None
) -> ExternalExport | None:
    raw = path.read_bytes()
    reader = csv.reader(raw.decode("utf-8-sig").splitlines())
    exported_account = _read_prefix(reader, account_number)
    if exported_account is None:
        if account_number is not None:
            return None
        raise ValueError("This file does not have the Saven transaction-export format.")

    transactions = []
    descriptions = []
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
                ",".join(row[3:]),
                dollar_prefix=True,
            )
        except ValueError as error:
            raise ValueError(
                f"Invalid transaction at row {row_number}: {error}"
            ) from None
        transactions.append((transaction_date, amount, balance))
        descriptions.append(row[1].strip())

    if not transactions:
        raise ValueError("The export has no transactions with a reported balance.")
    for newer, older in zip(transactions, transactions[1:]):
        if newer[0] < older[0]:
            raise ValueError("Expected transactions in newest-first order.")
        # Amounts are unsigned in this export. Balances establish movement direction.
        if abs(newer[2] - older[2]) != abs(newer[1]):
            raise ValueError("Transaction amounts do not reconcile adjacent balances.")

    return ExternalExport(
        account_number=exported_account,
        balance_cad=transactions[0][2],
        last_transaction_date=transactions[0][0],
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_modified_at=datetime.fromtimestamp(path.stat().st_mtime, UTC),
        transactions=tuple(
            ExternalTransaction(
                transaction_date,
                row_description,
                amount,
                balance,
                "interest" not in row_description.casefold()
                and re.search(r"\b(fees?|charges?)\b", row_description.casefold())
                is None,
            )
            for (transaction_date, amount, balance), row_description in zip(
                transactions, descriptions, strict=True
            )
        ),
    )


def find_latest_saven_export(
    directory: Path, account_number: str | None = None
) -> Path:
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
        raise ValueError(
            "Exports cover multiple accounts. Specify --account-number or --file."
        )
    latest_time = max(modified for _, _, modified in candidates)
    newest = [path for path, _, modified in candidates if modified == latest_time]
    if len(newest) != 1:
        raise ValueError("Multiple exports have the newest file time. Specify --file.")
    return newest[0]
