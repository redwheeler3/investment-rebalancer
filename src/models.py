"""Shared data types and constants used across the rebalancer."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


class ExternalExportNotFound(ValueError):
    """No export is available for a configured external account."""


@dataclass(frozen=True)
class ExternalTransaction:
    """One row in a provider-normalized, newest-first CAD ledger.

    amount_cad may be unsigned; adjacent balances establish movement direction.
    The adapter sets is_transfer to distinguish principal from interest and fees.
    """

    date: date
    description: str
    amount_cad: Decimal
    balance_cad: Decimal
    is_transfer: bool


@dataclass(frozen=True)
class ExternalExport:
    """Common account balance and ledger evidence returned by an adapter."""

    account_number: str
    balance_cad: Decimal
    last_transaction_date: date
    source_sha256: str
    source_modified_at: datetime
    transactions: tuple[ExternalTransaction, ...]

    def as_snapshot(self) -> dict:
        return {
            "balance_cad": str(self.balance_cad),
            "last_transaction_date": self.last_transaction_date.isoformat(),
            "source_sha256": self.source_sha256,
            "source_modified_at": self.source_modified_at.isoformat(),
        }


@dataclass
class TradeRecommendation:
    """A recommended trade to execute."""

    symbol: str
    action: str  # "BUY" or "SELL"
    quantity: int  # Whole shares
    account_number: str
    account_type: str
    owner: str
    price: float
    currency: str
    estimated_value: float  # price * quantity in native currency
    note: str = ""
    requires_fx: bool = False  # True if funded via cross-currency conversion


@dataclass
class TransferRecommendation:
    """A CAD transfer between an external holding and a broker account."""

    symbol: str
    action: str  # DEPOSIT into external savings, or WITHDRAW to the broker
    amount_cad: float
    account_number: str
    account_type: str
    owner: str
    requires_fx: bool = False
    in_progress: bool = False  # Already left the source; project its arrival only

    @property
    def estimated_value(self) -> float:
        return self.amount_cad

    @property
    def currency(self) -> str:
        return "CAD"


@dataclass
class TransferProgress:
    """Valuation adjustments supported by an outstanding transfer recommendation."""

    symbol: str
    action: str
    account_number: str
    account_type: str
    owner: str
    recommended_cad: float
    source_cad: float  # Observed source withdrawals, capped at the recommendation
    received_cad: float  # Actual destination receipts, including outside top-ups
    in_transit_cad: float  # Positive asset: recognized source money not yet received
    source_offset_cad: float = 0.0  # Positive amount to deduct from a stale source

    @property
    def transit_symbol(self) -> str:
        return f"TRANSIT:{self.symbol}"


@dataclass
class TransientAlert:
    """Alert about a transient holding in a specific account."""

    symbol: str
    quantity: float
    account_number: str
    account_type: str
    owner: str
    note: str


def get_transient_status(portfolio, transient_symbols: list) -> dict:
    """
    Identify which transient symbols are actually held and build alerts.

    Args:
        portfolio: PortfolioSummary with current holdings.
        transient_symbols: List of symbols from config to exclude.

    Returns:
        dict with:
            symbols:  set of transient symbols actually held
            alerts:   list of TransientAlert objects for display
    """
    if not transient_symbols:
        return {"symbols": set(), "alerts": []}

    held = set()
    alerts = []

    for symbol in transient_symbols:
        if symbol not in portfolio.holdings:
            continue  # Not held — nothing to do
        held.add(symbol)
        for acct in portfolio.accounts:
            for pos in acct.positions:
                if pos.symbol == symbol and pos.quantity > 0:
                    alerts.append(
                        TransientAlert(
                            symbol=symbol,
                            quantity=pos.quantity,
                            account_number=acct.number,
                            account_type=acct.account_type,
                            owner=acct.owner,
                            note="Excluded from rebalancing",
                        )
                    )

    return {"symbols": held, "alerts": alerts}
