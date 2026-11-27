"""Track cash moving between a configured external account and Questrade.

Each route has cumulative movement counters and one recommendation cycle. The
cycle records the counters at its start, so only subsequent movements count
toward its original cap. Persisted amounts are integer cents; portfolio-facing
amounts are CAD dollars.

The two institutions can report in either order. A source withdrawal adds a
transit asset until receipt; a Questrade receipt before an external export needs
a temporary deduction from the stale external balance instead. Neither case
changes the broker's reported cash.

Read the flow as: observe_external_balance / observe_broker -> progress -> apply.
After a successful local display, remember_recommendations arms future tracking.
"""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.broker_cash import BROKER_ZONE, cash_by_day
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
    # Questrade supplies no transfer ID. Ignore mutable descriptions and numeric
    # formatting; count identical signatures rather than assuming they are unique.
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
    """Encrypted observations and recommendation intent, keyed by external symbol.

    bank_in/out and broker_in/out are cumulative observed movements in cents.
    bank and broker_seen remember source evidence to prevent replay. A cycle's
    source_start/received_start are counter baselines, not account balances.
    DEPOSIT and WITHDRAW always describe the external account's direction.
    """

    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.path = path
        self.data = deepcopy(data) if data is not None else {"accounts": {}}
        if set(self.data) != {"accounts"} or not isinstance(
            self.data["accounts"], dict
        ):
            raise ValueError("Invalid transfer tracking state.")
        self.original = deepcopy(self.data)
        self.cash_baselines = {}
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
        if not isinstance(record, dict) or set(record) - {"pending_debit"} != required:
            raise ValueError("Invalid transfer account state.")
        if (
            type(record.get("pending_debit", 0)) is not int
            or record.get("pending_debit", 0) < 0
        ):
            raise ValueError("Invalid pending broker debit.")
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
                - {"balance_receipt", "balance_debit", "cash_baseline", "prior_debit"}
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
            for field, direction in (
                ("balance_receipt", "WITHDRAW"),
                ("balance_debit", "DEPOSIT"),
            ):
                if field in cycle and (
                    type(cycle[field]) is not int
                    or cycle[field] <= 0
                    or (field == "balance_receipt" and cycle[field] > cycle["limit"])
                    or cycle["action"] != direction
                ):
                    raise ValueError("Invalid inferred broker transfer amount.")
            if type(cycle.get("prior_debit", 0)) is not int or cycle.get(
                "prior_debit", 0
            ) < record.get("pending_debit", 0):
                raise ValueError("Invalid broker debit baseline.")
            if "cash_baseline" in cycle:
                baseline = cycle["cash_baseline"]
                if (
                    not isinstance(baseline, dict)
                    or set(baseline) - {"other_cash_cursor"}
                    not in (
                        {"day", "cents"},
                        {"day", "cents", "opening_gap", "posted_start"},
                        {
                            "day",
                            "cents",
                            "opening_gap",
                            "posted_start",
                            "matched_debits",
                        },
                    )
                    or type(baseline["cents"]) is not int
                ):
                    raise ValueError("Invalid broker cash baseline.")
                date.fromisoformat(baseline["day"])
                if "other_cash_cursor" in baseline:
                    cursor = baseline["other_cash_cursor"]
                    if (
                        "opening_gap" not in baseline
                        or not isinstance(cursor, dict)
                        or set(cursor) != {"net", "residual"}
                        or any(type(n) is not int for n in cursor.values())
                    ):
                        raise ValueError("Invalid non-transfer cash evidence.")
                if "opening_gap" in baseline:
                    gap = baseline["opening_gap"]
                    if (
                        type(gap) is not int
                        or not gap
                        or type(baseline["posted_start"]) is not int
                        or not (
                            0
                            <= baseline["posted_start"]
                            <= record["broker_in" if gap > 0 else "broker_out"]
                        )
                    ):
                        raise ValueError("Invalid pre-recommendation cash evidence.")
                    if "matched_debits" in baseline and (
                        gap >= 0
                        or type(baseline["matched_debits"]) is not int
                        or not 0
                        <= baseline["matched_debits"]
                        <= record["broker_out"] - baseline["posted_start"]
                    ):
                        raise ValueError("Invalid pre-recommendation debit match.")
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
        """Reconcile newest-first rows; only adapter-marked transfers add principal.

        A first observation establishes a baseline. Subsequent changed snapshots
        require ledger evidence continuous from the previous reported balance.
        Interest and fees change that balance without becoming transfer principal.
        """
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
            # Existing money is a starting balance, not a newly detected receipt.
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
            # Counts preserve genuinely repeated rows. Only the new prefix is
            # eligible: a wider date range must not replay older transactions.
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
            # A cached balance can establish the baseline without ledger rows.
            # Locate its closing balance in the export to identify later activity.
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
        prior_in = prior_out = 0
        cycle = record["cycle"]
        started = (
            datetime.fromisoformat(cycle["started_at"])
            .astimezone(exported.statement_timezone)
            .date()
            if cycle
            else None
        )
        # Walk chronologically. Balance differences establish the sign even when
        # an adapter's amount is unsigned. Validate the whole chain before saving.
        for row in reversed(new_rows):
            balance = cents(row.balance_cad)
            change = balance - running
            if abs(change) != abs(cents(row.amount_cad)):
                raise ValueError(
                    "External statements must include continuous activity since the tracked balance."
                )
            if row.is_transfer:
                if started and row.date < started:
                    prior_in += max(0, change)
                    prior_out += max(0, -change)
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
        # Historical movements advance lifetime totals and the cycle baseline equally.
        if cycle:
            if cycle["action"] == "WITHDRAW":
                cycle["source_start"] += prior_out
            else:
                cycle["received_start"] += prior_in
        record["bank_in"] += incoming
        record["bank_out"] += outgoing

    def observe_broker(self, symbol, account_number, activities, now=None):
        """Baseline old records and count newly posted CAD deposits/withdrawals once."""
        record = self.account(symbol)
        if record["broker_account"] not in (None, account_number):
            if self._unsettled(record) or self._pending_posting(record):
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
                if not additional:
                    continue
                increment = abs(amounts[key]) * additional
                cycle = record["cycle"]
                debit = amounts[key] < 0
                if debit and record.get("pending_debit"):
                    record["pending_debit"] = max(
                        0, record["pending_debit"] - increment
                    )
                if cycle is not None:
                    started = datetime.fromisoformat(cycle["started_at"])
                    # Late-posted historical activity is not evidence for this
                    # recommendation. Same-day order relies on the saved baseline.
                    if dates[key].date() < started.astimezone(dates[key].tzinfo).date():
                        if debit and cycle.get("prior_debit"):
                            cycle["prior_debit"] = max(
                                0, cycle["prior_debit"] - increment
                            )
                        continue
                record["broker_out" if debit else "broker_in"] += increment
        known = dict(previous or {})
        for key, count in observed.items():
            known[key] = max(known.get(key, 0), count)
        record.update(
            broker_account=account_number,
            broker_seen=known,
            checked_at=(now or datetime.now(UTC)).isoformat(),
        )
        cycle = record["cycle"]
        if cycle:
            field, posted = self._broker_evidence(record)
            if field in cycle and posted >= cycle[field]:
                del cycle[field]

    @staticmethod
    def _prior_broker_postings(record, residual=None):
        """Return debit and credit cents already in cash when this intent began."""
        cycle = record["cycle"]
        debit = cycle.get("prior_debit", 0) - record.get("pending_debit", 0)
        baseline = cycle.get("cash_baseline", {})
        gap = baseline.get("opening_gap", 0)
        if not gap:
            return debit, 0
        counter = record["broker_in" if gap > 0 else "broker_out"]
        new_postings = counter - baseline["posted_start"]
        matched = min(abs(gap), new_postings)
        if gap > 0:
            return debit, matched
        # A previous transfer's debit and today's opening gap may overlap.
        # Count both only to the extent that posted rows and unchanged cash
        # corroborate distinct debits. A reread cannot spend the evidence twice.
        lower = max(debit, matched)
        upper = min(new_postings, debit + matched)
        confirmed = min(baseline.get("matched_debits", 0), upper)
        if residual is not None:
            confirmed = max(confirmed, min(residual, upper))
            if confirmed > lower:
                baseline["matched_debits"] = confirmed
            else:
                baseline.pop("matched_debits", None)
        return max(lower, confirmed), 0

    def _broker_evidence(self, record):
        cycle = record["cycle"]
        debit, credit = self._prior_broker_postings(record)
        if cycle["action"] == "WITHDRAW":
            posted = record["broker_in"] - cycle["received_start"]
            return "balance_receipt", posted - credit
        posted = record["broker_out"] - cycle["source_start"]
        return "balance_debit", posted - debit

    def _unexplained_cash(self, record, cash, explained):
        """Remove cash effects that were already in the balance before intent."""
        baseline = record["cycle"]["cash_baseline"]
        residual = round(100 * (cash - explained)) - baseline["cents"]
        debit, credit = self._prior_broker_postings(record, residual)
        adjusted = residual + credit - debit
        gap = baseline.get("opening_gap", 0)
        if not gap:
            return adjusted

        # Changes in this net exclude EFTs. Executions and their subsequent
        # trade postings already describe a single debit or credit in explained.
        other_net = round(100 * explained) - (
            record["broker_in"] - record["broker_out"]
        )
        cursor = baseline.get("other_cash_cursor")
        if cursor is not None:
            sign = 1 if gap > 0 else -1
            newly_explained = sign * (other_net - cursor["net"])
            without_cash_change = sign * (cursor["residual"] - adjusted)
            matched = min(abs(gap), newly_explained, without_cash_change)
            if matched > 0:
                correction = sign * matched
                baseline["cents"] -= correction
                baseline["opening_gap"] -= correction
                if not baseline["opening_gap"]:
                    for key in (
                        "opening_gap",
                        "posted_start",
                        "matched_debits",
                        "other_cash_cursor",
                    ):
                        baseline.pop(key, None)
                residual += correction
                debit, credit = self._prior_broker_postings(record, residual)
                adjusted = residual + credit - debit
        if "opening_gap" in baseline:
            baseline["other_cash_cursor"] = {"net": other_net, "residual": adjusted}
        return adjusted

    def observe_broker_cash(self, symbol, endpoint, daily, day):
        """Infer the unposted cash movement in the intended direction, up to the cap."""
        record = self.account(symbol)
        cycle = record["cycle"]
        cash = Decimal(str(endpoint.cash_cad))
        # A new recommendation starts at this observation, excluding earlier
        # unexplained movements on the same day. Keep the basis across midnight.
        self.cash_baselines[symbol] = {
            "day": day.isoformat(),
            "cents": round(100 * (cash - daily.get(day, 0))),
        }
        opening = endpoint.start_of_day_cash_cad
        if opening is not None:
            snapshot = self.cash_baselines[symbol]
            gap = snapshot["cents"] - round(100 * Decimal(str(opening)))
            if gap:
                # Only postings first seen after this snapshot can explain
                # this already-observed, pre-recommendation cash movement.
                snapshot.update(
                    opening_gap=gap,
                    posted_start=record["broker_in" if gap > 0 else "broker_out"],
                    other_cash_cursor={
                        "net": round(100 * daily.get(day, 0))
                        - (record["broker_in"] - record["broker_out"]),
                        "residual": 0,
                    },
                )
        if not cycle:
            return
        if "cash_baseline" not in cycle:
            field, posted = self._broker_evidence(record)
            adjusted = self.cash_baselines[symbol]["cents"]
            opening_cents = (
                adjusted if opening is None else round(100 * Decimal(str(opening)))
            )
            # The day's opening may already include an observed, unposted EFT.
            # Preserve that evidence when establishing the comparison baseline.
            outstanding = max(0, cycle.get(field, 0) - posted)
            if outstanding:
                opening_cents = (
                    min(opening_cents, adjusted - outstanding)
                    if field == "balance_receipt"
                    else max(opening_cents, adjusted + outstanding)
                )
            cycle["cash_baseline"] = {
                "day": day.isoformat(),
                "cents": opening_cents,
            }
        baseline = cycle["cash_baseline"]
        start = date.fromisoformat(baseline["day"])
        explained = sum(value for d, value in daily.items() if d >= start)
        residual = self._unexplained_cash(record, cash, explained)
        field, posted = self._broker_evidence(record)
        cap = cycle["limit"]
        if field == "balance_receipt":
            source = record["bank_out"] - cycle["source_start"]
            if source:
                cap = min(cap, source)
            inferred = min(cap, posted + residual)
        else:
            # Keep the entire debit for matching a later EFT row. Only the
            # capped portion can become transit for this recommendation.
            inferred = posted - residual
        if inferred > posted:
            cycle[field] = inferred
        else:
            cycle.pop(field, None)

    def activity_start(self, symbol, now):
        """Cover recent activity, polling gaps and the active cycle, with a day of overlap."""
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
        """Return cumulative (source withdrawals, destination receipts) in cents."""
        return (
            (record["broker_out"], record["bank_in"])
            if action == "DEPOSIT"
            else (record["bank_out"], record["broker_in"])
        )

    def _amounts(self, record):
        """Return cycle (source, received, in_transit, stale_source_offset) in cents."""
        cycle = record["cycle"]
        if cycle is None:
            return None
        source_total, received_total = self._totals(record, cycle["action"])
        # Cap inferred source principal across the entire cycle. Actual receipts
        # stay uncapped: extra money may have come from the intermediate bank.
        posted_source = source_total - cycle["source_start"]
        posted_received = received_total - cycle["received_start"]
        _, posted = self._broker_evidence(record)
        if cycle["action"] == "DEPOSIT":
            posted_source = posted
        else:
            posted_received = posted
        source = min(cycle["limit"], max(cycle.get("balance_debit", 0), posted_source))
        received = max(0, posted_received, cycle.get("balance_receipt", 0))
        pending = max(0, source - received)
        awaiting = 0
        if cycle["action"] == "WITHDRAW" and source == 0:
            # Questrade has cash that the stale external balance still includes.
            # Once an export confirms a withdrawal, trust that source evidence;
            # larger receipts then represent outside-bank top-ups.
            awaiting = min(cycle["limit"], received)
        return source, received, pending, awaiting

    def _pending_posting(self, record):
        cycle = record["cycle"] or {}
        return (
            record.get("pending_debit", 0)
            or cycle.get("balance_debit", 0)
            or cycle.get("balance_receipt", 0)
        )

    def _unsettled(self, record):
        amounts = self._amounts(record)
        if amounts is None:
            return False
        _, _, transit, offset = amounts
        return bool(transit or offset or record["cycle"].get("balance_receipt"))

    def _close_cycle(self, record):
        # A statement can fulfill the deposit before the observed cash debit
        # posts. Carry only that known debit, never the outside-funded remainder.
        field, posted = self._broker_evidence(record)
        if field == "balance_debit":
            unposted = max(0, record["cycle"].get(field, 0) - posted)
            if unposted:
                record["pending_debit"] = record.get("pending_debit", 0) + unposted
        record["cycle"] = None

    def retain_routes(self, endpoints):
        """Allow removal only after cash has settled, preserving pending valuation."""
        for symbol, record in list(self.data["accounts"].items()):
            if symbol in endpoints:
                continue
            if self._unsettled(record) or self._pending_posting(record):
                raise ValueError(
                    "Keep an external target and its Questrade endpoint configured until its transfer settles."
                )
            del self.data["accounts"][symbol]

    def progress(self, symbol, endpoint):
        """Expose cycle amounts as CAD dollars for valuation, planning and display."""
        record = self.account(symbol)
        amounts = self._amounts(record)
        if amounts is None:
            return None
        source, received, pending, awaiting = amounts
        cycle = record["cycle"]
        field, posted = self._broker_evidence(record)
        unposted = max(0, cycle.get(field, 0) - posted)
        if cycle["action"] == "DEPOSIT":
            unposted = min(unposted, pending)
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
            unposted / 100,
        )

    def apply(self, portfolio, endpoints):
        """Add transit assets/temporary stale-source offsets without inventing broker cash."""
        if portfolio.transfer_progress:
            raise ValueError("Transfer adjustments are already applied.")
        for symbol, endpoint in endpoints.items():
            progress = self.progress(symbol, endpoint)
            if progress is None:
                continue
            if (
                progress.action == "DEPOSIT"
                and progress.received_cad >= progress.recommended_cad
            ):
                self._close_cycle(self.account(symbol))
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
                if self._unsettled(record):
                    # The original cap must survive until observed money settles.
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
                    # A settled partial payment can leave allowance for a later
                    # instalment, even if today's residual drift is below tolerance.
                    continue
                if (
                    not moved
                    and recommendation is not None
                    and recommendation.action == cycle["action"]
                ):
                    # Repeated reports must not resize an unexecuted cycle's cap.
                    continue
                self._close_cycle(record)
            if recommendation is not None and recommendation.amount_cad >= 0.01:
                # Counter baselines separate this intent from all prior movements.
                source, received = self._totals(record, recommendation.action)
                record["cycle"] = {
                    "action": recommendation.action,
                    "limit": cents(recommendation.amount_cad),
                    "source_start": source,
                    "received_start": received,
                    "started_at": (now or datetime.now(UTC)).isoformat(),
                }
                if record.get("pending_debit"):
                    record["cycle"]["prior_debit"] = record["pending_debit"]
                if symbol in self.cash_baselines:
                    record["cycle"]["cash_baseline"] = self.cash_baselines[symbol]


def reconcile_transfer_activity(portfolio, balances, clients, endpoints):
    """Observe both sides, adjust valuation, and persist before history/tactical use.

    External snapshots are already loaded; fresh export evidence is processed by
    the loader. Here Questrade activities complete the picture. This function
    reconciles existing intent; it does not arm new recommendations.
    """
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
    for symbol, endpoint in endpoints.items():
        # Quotes and other accounts may have been fetched since the balance call.
        # An execution after that snapshot cannot explain its cash.
        now = endpoint.cash_observed_at or datetime.now(UTC)
        today = now.astimezone(BROKER_ZONE).date()
        client = client_accounts[endpoint.number]
        start = ledger.activity_start(symbol, now)
        first_start = start
        counts = Counter()
        examples = {}
        cycle = ledger.account(symbol)["cycle"]
        baseline = (cycle or {}).get("cash_baseline")
        check_cash = (
            endpoint.cash_observed_at is not None
            or endpoint.start_of_day_cash_cad is not None
            or baseline is not None
        )
        cash_day = date.fromisoformat(baseline["day"]) if baseline else today
        cash_start = datetime.combine(cash_day, time.min, BROKER_ZONE)
        cash_rows = []
        executions = {}
        while start < now:
            # The API permits at most 31 days; 30-day windows stay within it.
            end = min(start + timedelta(days=30), now)
            rows = client.get_activities(
                endpoint.number, start.isoformat(), end.isoformat()
            )
            window = Counter()
            for row in rows:
                if check_cash and row.get("currency") == "CAD" and row.get("netAmount"):
                    posted = _posted_at(row)
                    if max(start, cash_start) <= posted <= end and (
                        start == first_start or posted != start
                    ):
                        cash_rows.append(row)
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
                # Shared window boundaries can repeat records. Use the largest
                # observed multiplicity, not the sum across overlapping windows.
                counts[key] = max(counts[key], count)
            if check_cash and end > cash_start:
                for fill in client.get_executions(
                    endpoint.number, max(start, cash_start).isoformat(), end.isoformat()
                ):
                    executions[fill["id"]] = fill
            start = end
        rows = [examples[key] for key, count in counts.items() for _ in range(count)]
        ledger.observe_broker(symbol, endpoint.number, rows, now)
        if check_cash:
            currencies = {p.symbol_id: p.currency for p in endpoint.positions}
            missing = {f["symbolId"] for f in executions.values()} - currencies.keys()
            if missing:
                currencies.update(
                    {
                        s["symbolId"]: s["currency"]
                        for s in client.get_symbols(sorted(missing))
                    }
                )
            daily = cash_by_day(cash_rows, executions.values(), currencies)
            ledger.observe_broker_cash(symbol, endpoint, daily, today)
    ledger.apply(portfolio, endpoints)
    ledger.save()
    return ledger
