"""Reconcile recommended external transfers without changing reported account cash."""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.encrypted_state import read_encrypted_json, write_encrypted_json
from src.models import TransferProgress, TransferRecommendation
from src.paths import get_data_dir
from src.portfolio import HoldingSummary


def cents(value) -> int:
    """Require finite, whole-cent amounts at the persistence/API boundary."""
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError("Invalid CAD precision.")
        return int(amount)
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        raise ValueError(
            "Transfer amounts must be finite CAD values with cent precision."
        ) from None


def _signature(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _broker_signature(row):
    return _signature(
        [
            _posted_at(row).astimezone(UTC).isoformat(),
            str(row["type"]).casefold(),
            row["currency"],
            cents(row["netAmount"]),
        ]
    )


def _posted_at(row):
    try:
        posted = datetime.fromisoformat(row["transactionDate"])
        if posted.utcoffset() is None:
            raise ValueError("Missing activity timezone.")
        return posted
    except (KeyError, TypeError, ValueError):
        raise ValueError(
            "Questrade transfer activity requires a dated posting record."
        ) from None


def _bank_signature(row):
    return _signature(
        [
            row.date.isoformat(),
            row.description,
            cents(row.amount_cad),
            cents(row.balance_cad),
        ]
    )


class TransferLedger:
    """Small encrypted observation cursors and one recommendation cycle per route."""

    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.path = path
        self.data = deepcopy(data) if data is not None else {"accounts": {}}
        if set(self.data) != {"accounts"} or not isinstance(
            self.data["accounts"], dict
        ):
            raise ValueError("Invalid transfer tracking state.")
        self.original = deepcopy(self.data)
        for record in self.data["accounts"].values():
            self._validate(record)

    @classmethod
    def load(cls, path: Path | None = None):
        path = path if path is not None else get_data_dir() / "transfer_tracking.json"
        return cls(read_encrypted_json(path) if path.exists() else None, path)

    def save(self):
        if self.path is None:
            raise ValueError("A transfer ledger path is required for persistence.")
        for record in self.data["accounts"].values():
            self._validate(record)
        if self.data != self.original:
            write_encrypted_json(self.path, self.data)
            self.original = deepcopy(self.data)

    @staticmethod
    def _validate(record):
        required = {
            "bank",
            "bank_in",
            "bank_out",
            "broker_in",
            "broker_out",
            "broker_account",
            "broker_seen",
            "checked_at",
            "cycle",
        }
        if not isinstance(record, dict) or set(record) != required:
            raise ValueError("Invalid transfer account state.")
        for key in ("bank_in", "bank_out", "broker_in", "broker_out"):
            if type(record[key]) is not int or record[key] < 0:
                raise ValueError("Invalid cumulative transfer amount.")
        if record["bank"] is not None:
            bank = record["bank"]
            if (
                not isinstance(bank, dict)
                or set(bank) != {"hash", "balance", "date", "seen"}
                or type(bank["balance"]) is not int
                or bank["balance"] < 0
            ):
                raise ValueError("Invalid transfer bank cursor.")
            datetime.fromisoformat(bank["date"])
            if bank["seen"] is not None and (
                not isinstance(bank["seen"], dict)
                or any(
                    type(count) is not int or count < 1
                    for count in bank["seen"].values()
                )
            ):
                raise ValueError("Invalid external activity cursor.")
        seen = record["broker_seen"]
        if seen is not None and (
            not isinstance(seen, dict)
            or any(type(count) is not int or count < 1 for count in seen.values())
        ):
            raise ValueError("Invalid transfer activity cursor.")
        if (
            record["checked_at"] is not None
            and datetime.fromisoformat(record["checked_at"]).utcoffset() is None
        ):
            raise ValueError("Transfer activity dates require timezones.")
        cycle = record["cycle"]
        if cycle is not None:
            if (
                not isinstance(cycle, dict)
                or set(cycle)
                != {
                    "action",
                    "limit",
                    "source_start",
                    "received_start",
                    "started_at",
                }
                or cycle["action"] not in {"DEPOSIT", "WITHDRAW"}
            ):
                raise ValueError("Invalid outstanding transfer recommendation.")
            if (
                any(
                    type(cycle[key]) is not int or cycle[key] < 0
                    for key in ("limit", "source_start", "received_start")
                )
                or cycle["limit"] == 0
            ):
                raise ValueError("Invalid transfer recommendation amounts.")
            if datetime.fromisoformat(cycle["started_at"]).utcoffset() is None:
                raise ValueError("Transfer recommendation dates require timezones.")
            source_key, received_key = (
                ("broker_out", "bank_in")
                if cycle["action"] == "DEPOSIT"
                else ("bank_out", "broker_in")
            )
            if (
                record["bank"] is None
                or record["broker_seen"] is None
                or record["broker_account"] is None
                or cycle["source_start"] > record[source_key]
                or cycle["received_start"] > record[received_key]
            ):
                raise ValueError(
                    "Transfer recommendation has inconsistent observation baselines."
                )

    def account(self, symbol):
        return self.data["accounts"].setdefault(
            symbol,
            {
                "bank": None,
                "bank_in": 0,
                "bank_out": 0,
                "broker_in": 0,
                "broker_out": 0,
                "broker_account": None,
                "broker_seen": None,
                "checked_at": None,
                "cycle": None,
            },
        )

    def observe_external_balance(self, symbol, snapshot, exported=None):
        """Process a changed ledger against the previous reported balance, excluding interest."""
        record = self.account(symbol)
        current = {
            "hash": snapshot["source_sha256"],
            "balance": cents(snapshot["balance_cad"]),
            "date": snapshot["last_transaction_date"],
            "seen": dict(Counter(_bank_signature(row) for row in exported.transactions))
            if exported is not None
            else None,
        }
        prior = record["bank"]
        if prior is None:
            record["bank"] = current
            return
        if current["hash"] == prior["hash"]:
            if prior["seen"] is None and current["seen"] is not None:
                record["bank"] = current
            return
        if exported is None or exported.source_sha256 != current["hash"]:
            raise ValueError(
                "New external balance requires statement evidence; refresh the export locally."
            )
        rows = list(exported.transactions)
        if prior["seen"] is not None:
            new_counts = Counter(current["seen"]) - Counter(prior["seen"])
            new_rows = []
            for row in rows:
                key = _bank_signature(row)
                if row.date.isoformat() < prior["date"] or new_counts[key] <= 0:
                    break
                new_rows.append(row)
                new_counts[key] -= 1
            current["seen"] = dict(Counter(prior["seen"]) | Counter(current["seen"]))
        else:
            anchor = next(
                (
                    index
                    for index, row in enumerate(rows)
                    if row.date.isoformat() == prior["date"]
                    and cents(row.balance_cad) == prior["balance"]
                ),
                None,
            )
            new_rows = (
                rows[:anchor]
                if anchor is not None
                else [row for row in rows if row.date.isoformat() >= prior["date"]]
            )
        running = prior["balance"]
        incoming = outgoing = 0
        for row in reversed(new_rows):
            balance = cents(row.balance_cad)
            change = balance - running
            if abs(change) != abs(cents(row.amount_cad)):
                raise ValueError(
                    "External statements must include continuous activity since the tracked balance."
                )
            if row.is_transfer:
                if change > 0:
                    incoming += change
                else:
                    outgoing -= change
            running = balance
        if running != current["balance"]:
            raise ValueError(
                "External statement does not reconcile the tracked balance."
            )
        record["bank"] = current
        record["bank_in"] += incoming
        record["bank_out"] += outgoing

    def observe_broker(self, symbol, account_number, activities, now=None):
        """Baseline old records and count newly posted CAD deposits/withdrawals once."""
        record = self.account(symbol)
        if record["broker_account"] not in (None, account_number):
            amounts = self._amounts(record)
            if amounts is not None and (amounts[2] > 0 or amounts[3] > 0):
                raise ValueError(
                    "Cannot change a broker endpoint with an outstanding transfer."
                )
            record["cycle"] = None
            record["broker_seen"] = None
            record["broker_in"] = record["broker_out"] = 0
        observed = Counter()
        amounts = {}
        dates = {}
        for row in activities:
            kind = str(row.get("type", "")).casefold()
            if (
                row.get("currency") != "CAD"
                or row.get("symbol")
                or kind not in {"deposits", "withdrawals"}
            ):
                continue
            amount = cents(row["netAmount"])
            if (kind == "withdrawals" and amount >= 0) or (
                kind == "deposits" and amount <= 0
            ):
                raise ValueError(
                    "Questrade cash activity has an inconsistent direction."
                )
            key = _broker_signature(row)
            observed[key] += 1
            amounts[key] = amount
            dates[key] = _posted_at(row)
        previous = record["broker_seen"]
        if previous is not None:
            for key, count in observed.items():
                additional = max(0, count - previous.get(key, 0))
                if record["cycle"] is not None:
                    started = datetime.fromisoformat(record["cycle"]["started_at"])
                    if dates[key].date() < started.astimezone(dates[key].tzinfo).date():
                        continue
                amount = amounts[key]
                record["broker_in" if amount > 0 else "broker_out"] += (
                    abs(amount) * additional
                )
        known = dict(previous or {})
        for key, count in observed.items():
            known[key] = max(known.get(key, 0), count)
        record.update(
            broker_account=account_number,
            broker_seen=known,
            checked_at=(now or datetime.now(UTC)).isoformat(),
        )

    def activity_start(self, symbol, now):
        record = self.account(symbol)
        starts = [now - timedelta(days=30)]
        if record["checked_at"]:
            starts.append(
                datetime.fromisoformat(record["checked_at"]) - timedelta(days=1)
            )
        if record["cycle"]:
            starts.append(
                datetime.fromisoformat(record["cycle"]["started_at"])
                - timedelta(days=1)
            )
        return min(starts)

    def _totals(self, record, action):
        return (
            (record["broker_out"], record["bank_in"])
            if action == "DEPOSIT"
            else (record["bank_out"], record["broker_in"])
        )

    def _amounts(self, record):
        cycle = record["cycle"]
        if cycle is None:
            return None
        source_total, received_total = self._totals(record, cycle["action"])
        source = min(cycle["limit"], max(0, source_total - cycle["source_start"]))
        received = max(0, received_total - cycle["received_start"])
        pending = max(0, source - received)
        awaiting = 0
        if cycle["action"] == "WITHDRAW" and source == 0:
            awaiting = min(cycle["limit"], received)
        return source, received, pending, awaiting

    def retain_routes(self, endpoints):
        """Allow removal only after cash has settled, preserving pending valuation."""
        for symbol, record in list(self.data["accounts"].items()):
            if symbol in endpoints:
                continue
            amounts = self._amounts(record)
            if amounts is not None and (amounts[2] > 0 or amounts[3] > 0):
                raise ValueError(
                    "Keep an external target and its Questrade endpoint configured until its transfer settles."
                )
            del self.data["accounts"][symbol]

    def progress(self, symbol, endpoint):
        record = self.account(symbol)
        amounts = self._amounts(record)
        if amounts is None:
            return None
        source, received, pending, awaiting = amounts
        cycle = record["cycle"]
        return TransferProgress(
            symbol,
            cycle["action"],
            endpoint.number,
            endpoint.account_type,
            endpoint.owner,
            cycle["limit"] / 100,
            source / 100,
            received / 100,
            pending / 100,
            awaiting / 100,
        )

    def apply(self, portfolio, endpoints):
        """Add transit assets/temporary stale-source offsets without inventing broker cash."""
        if portfolio.transfer_progress:
            raise ValueError("Transfer adjustments are already applied.")
        for symbol, endpoint in endpoints.items():
            progress = self.progress(symbol, endpoint)
            if progress is None:
                continue
            portfolio.transfer_progress.append(progress)
            if progress.source_offset_cad:
                holding = portfolio.holdings[symbol]
                if progress.source_offset_cad > holding.value_cad:
                    raise ValueError(
                        "An external source adjustment exceeds its reported balance."
                    )
                holding.value_cad -= progress.source_offset_cad
                portfolio.total_value_cad -= progress.source_offset_cad
            if progress.in_transit_cad:
                portfolio.holdings[progress.transit_symbol] = HoldingSummary(
                    value_cad=progress.in_transit_cad
                )
                portfolio.total_value_cad += progress.in_transit_cad

    def remember_recommendations(self, actions, endpoints, now=None):
        """Retain an active cycle's cap; replace only unused or fully reconciled intent."""
        recommendations = {
            action.symbol: action
            for action in actions
            if isinstance(action, TransferRecommendation) and not action.in_progress
        }
        for symbol, endpoint in endpoints.items():
            record = self.account(symbol)
            if record["bank"] is None or record["broker_seen"] is None:
                raise ValueError(
                    "Transfer observations must be baselined before recommendations."
                )
            recommendation = recommendations.get(symbol)
            progress = self.progress(symbol, endpoint)
            cycle = record["cycle"]
            if cycle is not None:
                moved = progress.source_cad > 0 or progress.received_cad > 0
                pending = progress.in_transit_cad > 0 or progress.source_offset_cad > 0
                if moved and pending:
                    continue
                if (
                    moved
                    and progress.source_cad < progress.recommended_cad
                    and progress.received_cad < progress.recommended_cad
                    and (
                        recommendation is None
                        or recommendation.action == cycle["action"]
                    )
                ):
                    continue
                if (
                    not moved
                    and recommendation is not None
                    and recommendation.action == cycle["action"]
                ):
                    continue
                record["cycle"] = None
            if recommendation is not None and recommendation.amount_cad >= 0.01:
                source, received = self._totals(record, recommendation.action)
                record["cycle"] = {
                    "action": recommendation.action,
                    "limit": cents(recommendation.amount_cad),
                    "source_start": source,
                    "received_start": received,
                    "started_at": (now or datetime.now(UTC)).isoformat(),
                }


def reconcile_transfer_activity(portfolio, balances, clients, endpoints):
    """Reconcile before history/tactical evaluation in local and scheduled runs."""
    path = get_data_dir() / "transfer_tracking.json"
    if not endpoints and not path.exists():
        return None
    ledger = TransferLedger.load(path)
    ledger.retain_routes(endpoints)
    if not endpoints:
        ledger.save()
        return None
    for symbol in endpoints:
        ledger.observe_external_balance(symbol, balances[symbol])
    client_accounts = {}
    needed_accounts = {endpoint.number for endpoint in endpoints.values()}
    for client in clients:
        for account in client.get_accounts():
            if account["number"] not in needed_accounts:
                continue
            if account["number"] in client_accounts:
                raise ValueError(
                    "A transfer account appears under multiple broker connections."
                )
            client_accounts[account["number"]] = client
    if len({endpoint.number for endpoint in endpoints.values()}) != len(endpoints):
        raise ValueError(
            "Each external account requires a distinct Questrade transfer endpoint."
        )
    now = datetime.now(UTC)
    for symbol, endpoint in endpoints.items():
        client = client_accounts[endpoint.number]
        start = ledger.activity_start(symbol, now)
        counts = Counter()
        examples = {}
        while start < now:
            end = min(start + timedelta(days=30), now)
            rows = client.get_activities(
                endpoint.number, start.isoformat(), end.isoformat()
            )
            window = Counter()
            for row in rows:
                if (
                    str(row.get("type", "")).casefold()
                    not in {"deposits", "withdrawals"}
                    or row.get("currency") != "CAD"
                    or row.get("symbol")
                ):
                    continue
                key = _broker_signature(row)
                window[key] += 1
                examples[key] = row
            for key, count in window.items():
                counts[key] = max(counts[key], count)
            start = end
        rows = [examples[key] for key, count in counts.items() for _ in range(count)]
        ledger.observe_broker(symbol, endpoint.number, rows, now)
    ledger.apply(portfolio, endpoints)
    ledger.save()
    return ledger
