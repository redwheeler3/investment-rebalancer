# Rebalancer Algorithm Deep Dive

How `src/rebalancer.py` decides what to buy, sell, and where — following the actual code path.

---

## Design Philosophy

The rebalancer operates at the **household level** — it sees all accounts as one portfolio, measures drift against unified targets, then figures out which specific accounts to trade in. This matches how a family thinks about allocation ("we want 53% in Canadian S&P 500") without being constrained by per-account limitations.

---

## Entry Point

Everything starts with one call from `main.py`:

```python
trades = calculate_trades(
    portfolio,
    resolved_targets,
    usd_to_cad_rate,
    norberts_gambit_fee_cad,
    drift_trade_threshold_pct,
    transient_symbols=hidden_symbols,
    dlr_quotes=dlr_quotes,
)
```

This creates a `RebalancePlanner` and calls `planner.build()`.

---

## Planner Initialization

Before any trades are planned, `__post_init__` sets up the working state:

```python
def __post_init__(self) -> None:
    # Calculate current allocations (excluding transient symbols like DLR.TO)
    current_alloc = get_current_allocations(
        self.portfolio, self.usd_to_cad_rate, excluded_symbols=self.hidden_symbols
    )

    # Compute initial drift: how far each symbol is from target
    self.initial_drifts = get_drifts(current_alloc, self.targets)

    # Holdings view without transient symbols (this is what we rebalance)
    self.holdings_view = get_holdings_view(self.portfolio, self.hidden_symbols)

    # TradePlan: accumulates trades, projects future drifts
    self.plan = TradePlan(...)

    # CashLedger: tracks per-account, per-currency cash as we plan
    self.ledger = CashLedger.from_portfolio(self.portfolio, ...)
```

At this point we know: VSP.TO is +2.3% over target, IVV is -1.9% under, etc.

---

## The Main Loop

```python
def build(self) -> list:
    for _ in range(MAX_ROUNDS):     # Up to 10 iterations
        starter_changes = 0
        starter_changes += self._sell_overweight_starters()
        starter_changes += self._buy_underweight_starters()

        if starter_changes > 0:
            self._deploy_residual_cash()

        if starter_changes == 0:
            break                    # Converged — no more drift to fix

    return self.plan.netted_trades()  # Consolidate and return
```

**Why iterate?** Selling VSP.TO (overweight) gives you cash to buy IVV (underweight). But whole-share rounding means the drift won't close perfectly. Round 2 catches the leftovers. In practice: 1-2 rounds.

---

## Layer 1: Sell Overweight (`_sell_overweight_starters`)

### Step 1: Find overweight symbols

```python
drifts = self.plan.drifts() if self.plan.trades else dict(self.initial_drifts)
overweight_symbols = [
    (symbol, drift_pct)
    for symbol, drift_pct in drifts.items()
    if symbol not in ("CAD", "USD") and drift_pct > self.drift_trade_threshold_pct
]
overweight_symbols.sort(key=lambda item: item[1], reverse=True)  # Most overweight first
```

**Why most-overweight first?** Selling the most bloated position first frees up the most cash for subsequent buys.

**Why re-check `self.plan.drifts()` vs the initial snapshot?** In round 2+, the projected drifts after prior trades may be different from where we started.

### Step 2: Size the sell

```python
shares = shares_for_drift_gap(
    self.portfolio.total_value_cad,  # $1,000,000
    current_drift,                   # +2.3%
    bid_price_native,                # $114.14
    currency,                        # "CAD"
    self.usd_to_cad_rate,
)
```

Inside `shares_for_drift_gap`:

```python
gap_cad = abs(drift_pct / 100.0) * total_value_cad   # 2.3% × $1M = $23,000
gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
shares = int(math.floor(gap_native / price_native))    # floor($23,000 / $114.14) = 201
```

Uses `floor()` — we'd rather undershoot (sell 201 shares) than overshoot (sell 202 and go underweight).

**Edge case — the "one share" rule:**
```python
if shares == 0:
    one_share_cad = to_cad(price_native, currency, usd_to_cad_rate)
    if one_share_cad < 2 * gap_cad:
        shares = 1  # Close enough — trade 1 share
```
If the drift gap is small but a single share would roughly close it, go ahead.

### Step 3: Decide which accounts to sell from

```python
sell_trades = allocate_sell(
    symbol, shares, bid_price_native, currency,
    self.portfolio.accounts,
    effective_drift=self.plan.drifts(),
    transient_symbols=self.hidden_symbols,
    drift_trade_threshold_pct=self.drift_trade_threshold_pct,
    position_deltas=self.plan.position_deltas(),
)
```

Inside `allocate_sell`:

```python
holders = find_accounts_for_symbol(symbol, accounts)

# Sort by: (1) has underweight alternatives? (2) position size
holders.sort(
    key=lambda a: (
        1 if _has_underweight_alternatives(a, symbol, ...) else 0,
        _effective_qty(a, symbol, position_deltas),
    ),
    reverse=True,
)
```

**What `_has_underweight_alternatives` checks:** Can this account's sell proceeds be immediately redeployed? It scans the account's other positions for anything with drift below -threshold.

**What `_effective_qty` does:** Returns the position size *adjusted for trades already planned this round*. Prevents over-selling when multiple rounds target the same account.

```python
def _effective_qty(account, symbol, position_deltas):
    original = int(get_position_quantity(account, symbol))
    delta = position_deltas.get((account.number, symbol), 0)
    return max(0, original + delta)
```

### Step 4: Record the trades

```python
for trade in sell_trades:
    self.ledger.credit_sale(trade.account_number, currency, trade.estimated_value)
    self.plan.add_trade(trade)
```

`credit_sale` adds proceeds to the account's cash. `add_trade` adds to the plan AND invalidates the cached projection (so next `self.plan.drifts()` call recalculates).

---

## Layer 2: Buy Underweight (`_buy_underweight_starters`)

### Step 1: Find underweight symbols

```python
underweight_symbols = [
    (symbol, drift_pct)
    for symbol, drift_pct in drifts.items()
    if symbol not in ("CAD", "USD") and drift_pct < -self.drift_trade_threshold_pct
]
underweight_symbols.sort(key=lambda item: item[1])  # Most underweight first
```

### Step 2: For each symbol, try to buy (`_buy_symbol_toward_target`)

```python
shares_needed = shares_for_drift_gap(...)  # Same math as sells, but uses ask price
eligible_accounts = self._eligible_buy_accounts(symbol, currency)

remaining = shares_needed
for acct in eligible_accounts:
    quantity = self._buy_in_account(acct, symbol, ask_price, currency, remaining)
    remaining -= quantity
```

### Step 3: Account prioritization (`_eligible_buy_accounts`)

```python
def sort_key(account):
    same_currency = self.ledger.same_currency_buying_power(account.number, currency)
    total = self.ledger.total_buying_power(account.number, currency, dlr_quotes=...)
    return (
        1 if same_currency > 0 else 0,  # Prefer accounts with matching cash
        same_currency,                    # Among those, most cash first
        total,                            # Tiebreaker: total buying power
    )
```

**Key rule:** Only accounts that already hold the symbol are eligible. You can't put IVV in an account that doesn't have it.

### Step 4: Execute the buy in an account (`_buy_in_account`)

```python
buying_power = self.ledger.total_buying_power(acct.number, currency, dlr_quotes=...)

# Not enough? Try raising cash first
if buying_power < ask_price_native:
    self._raise_cash_in_account(acct, symbol, currency, ask_price_native)
    buying_power = self.ledger.total_buying_power(...)  # Recalculate

affordable = int(math.floor(buying_power / ask_price_native))
quantity = min(remaining_shares, affordable)

# Fund the purchase (returns True if cross-currency conversion was needed)
converted = self.ledger.fund_buy(acct.number, currency, cost_native, dlr_quotes=...)

self.plan.add_trade(TradeRecommendation(
    ...,
    note="Underweight buy (requires FX)" if converted else "Underweight buy",
    requires_fx=converted,
))
```

### The `fund_buy` mechanism inside `CashLedger`

This is where the CAD-vs-USD decision happens:

```python
def fund_buy(self, account_number, currency, cost_native, dlr_quotes=None) -> bool:
    native_cash = self.cash(account_number, currency)

    if native_cash >= cost_native:
        # Easy path: pay with same-currency cash
        self.balances[account_number][currency] -= cost_native
        return False  # No FX needed

    # Hard path: use native cash + convert the remainder
    remainder_native = cost_native - native_cash
    self.balances[account_number][currency] = 0.0
    source_currency = "CAD" if currency == "USD" else "USD"
    consume_cross_currency_cash(
        self.balances[account_number],
        source_currency, currency, remainder_native, ...
    )
    return True  # FX was needed
```

---

## Cash Raising: Displacement Sells (`_raise_cash_in_account`)

When an account wants to buy IVV (USD) but has no USD and no CAD to convert, the planner can sell overweight *same-currency* holdings in that account:

```python
def _raise_cash_in_account(self, acct, buy_symbol, currency, minimum_needed_native):
    drifts = self.plan.drifts()
    candidates = []

    for pos in acct.positions:
        # Skip: the symbol we're trying to buy, transients, wrong currency, not overweight
        if pos.symbol == buy_symbol or pos.symbol in self.hidden_symbols:
            continue
        if pos.quantity <= 0 or pos.currency != currency:
            continue
        drift_pct = drifts.get(pos.symbol, 0.0)
        if drift_pct <= 0:
            continue  # Only sell things that are overweight

        # How much can we sell without making this symbol underweight?
        max_sellable = min(
            self.plan.effective_qty(acct, pos.symbol),
            max_sellable_without_crossing_target(...)
        )
        candidates.append((drift_pct, pos.symbol, bid_price, max_sellable))
```

**`max_sellable_without_crossing_target`** — this is the safety valve:

```python
def max_sellable_without_crossing_target(total_value_cad, drift_pct, price, currency, rate):
    per_share_drift_pct = (to_cad(price, currency, rate) / total_value_cad) * 100.0
    return int(math.floor(drift_pct / per_share_drift_pct))
```

It calculates: "each share I sell reduces drift by X%. I have Y% of positive drift. So I can sell at most Y/X shares before going negative."

Then it sells the most overweight candidates first, stopping as soon as enough cash is raised:

```python
candidates.sort(key=lambda item: item[0], reverse=True)  # Most overweight first
for _drift_pct, symbol, bid_price, max_sellable in candidates:
    shortfall = minimum_needed_native - current_cash
    if shortfall <= 0:
        break  # We have enough

    sell_qty = min(max_sellable, ceil(shortfall / bid_price))
    # Create "Displacement sell" trade...
```

---

## Layer 3: Residual Cash Deployment (`_deploy_residual_cash`)

After starter sells and buys, cash can remain in accounts from rounding, pre-existing balances, or partial fills. This layer minimizes idle cash.

### The two-pass approach

```python
def _deploy_residual_cash(self) -> None:
    while True:
        made_trade = False
        projected_drifts = self.plan.drifts()

        for acct in self.portfolio.accounts:
            # Pass 1: Same-currency buys (no conversion cost)
            for currency in ("CAD", "USD"):
                while True:
                    trade = build_same_currency_buy(...)    # From cash_deploy.py
                    if trade is None:
                        trade = self._build_cash_minimizing_same_currency_buy(...)
                    if trade is None:
                        break
                    self.plan.add_trade(trade)
                    made_trade = True

            # Pass 2: Cross-currency buys (conversion needed)
            for source_currency in ("CAD", "USD"):
                while True:
                    trade = build_cross_currency_buy(...)   # From cash_deploy.py
                    if trade is None:
                        trade = self._build_cash_minimizing_cross_currency_buy(...)
                    if trade is None:
                        break
                    self.plan.add_trade(trade)
                    made_trade = True

        if not made_trade:
            break  # No more cash to deploy anywhere
```

### The fallback chain for each account/currency

1. **`build_same_currency_buy`** (from `cash_deploy.py`) — Buy the most underweight symbol in this account that matches the currency. Caps at the number of shares needed to close the gap.

2. **`_build_cash_minimizing_same_currency_buy`** — Fallback if no underweight symbols exist. Buys the *least overweight* symbol — the goal is "don't leave cash idle" not "fix drift."

3. **`build_cross_currency_buy`** — Convert other-currency cash and buy the most underweight symbol.

4. **`_build_cash_minimizing_cross_currency_buy`** — Same fallback logic but with conversion.

### How `cash_deploy.py` picks candidates

```python
def underweight_candidates(acct, holdings, drifts, hidden_symbols, currency, threshold):
    candidates = []
    for pos in acct.positions:
        if pos.quantity <= 0 or pos.currency != currency:
            continue
        if pos.symbol in hidden_symbols:
            continue
        drift_pct = drifts.get(pos.symbol, 0.0)
        if drift_pct >= -threshold:
            continue  # Not underweight enough

        ask_price = holdings.get(pos.symbol).ask_price
        candidates.append((pos.symbol, drift_pct, ask_price))

    candidates.sort(key=lambda item: item[1])  # Most underweight first
    return candidates
```

### The "best available" fallback (`_account_buyable_candidates`)

When `underweight_only=False`, this returns ALL buyable symbols sorted by drift — the least overweight / most underweight one wins:

```python
def _account_buyable_candidates(self, acct, currency, drifts, *, underweight_only):
    for pos in acct.positions:
        # Must: have quantity, match currency, not hidden, have a target > 0
        if self.targets.get(pos.symbol, 0.0) <= 0:
            continue  # Don't buy things that aren't in our target allocation

        drift_pct = drifts.get(pos.symbol, 0.0)
        if underweight_only and drift_pct >= 0:
            continue

        candidates.append((drift_pct, pos.symbol, ask_price))

    candidates.sort(key=lambda item: item[0])  # Lowest drift first
    return candidates
```

---

## Trade Netting

The planner generates trades across multiple rounds and layers. A single (symbol, account) pair might get:
- A sell in Layer 1
- A buy-back in Layer 3 (leftover cash)

`net_trades()` consolidates:

```python
def net_trades(all_trades: list) -> list:
    position_map = {}  # (symbol, account_number) -> [trades...]
    for trade in all_trades:
        key = (trade.symbol, trade.account_number)
        position_map.setdefault(key, []).append(trade)

    final_trades = []
    for (symbol, account_number), trades_list in position_map.items():
        total_buy_qty = sum(t.quantity for t in trades_list if t.action == "BUY")
        total_sell_qty = sum(t.quantity for t in trades_list if t.action == "SELL")

        net_quantity = total_buy_qty - total_sell_qty
        if net_quantity > 0:
            # Net buy — use the buy price, first buy note
            ...
        elif net_quantity < 0:
            # Net sell — use the sell price, first sell note
            ...
        # If net_quantity == 0: trades cancel out, nothing emitted
```

---

## The `TradePlan` State Object

This is the planner's "memory" — it knows what's been planned and what the portfolio will look like after:

```python
@dataclass
class TradePlan:
    portfolio: object
    targets: dict
    usd_to_cad_rate: float
    hidden_symbols: set
    trades: list = field(default_factory=list)
    _netted_cache: list | None = None     # Invalidated on each add_trade
    _snapshot_cache: object | None = None  # Invalidated on each add_trade

    def drifts(self) -> dict:
        """Project: if we did all these trades, what would drift look like?"""
        return dict(self.projected_snapshot().drifts)

    def projected_snapshot(self):
        """Run simulate_rebalance with netted trades — cached until invalidated."""
        if self._snapshot_cache is None:
            self._snapshot_cache = simulate_rebalance(
                self.portfolio, self.netted_trades(), self.targets, ...
            )
        return self._snapshot_cache

    def add_trade(self, trade):
        self.trades.append(trade)
        self._netted_cache = None      # Force re-netting
        self._snapshot_cache = None    # Force re-projection
```

**Why cache?** `drifts()` is called many times per round (once per symbol being evaluated). Rerunning `simulate_rebalance` each time would be expensive. The cache makes repeated calls O(1) until the next trade is added.

---

## The `CashLedger` State Object

Tracks what cash is available in each account as trades are planned:

```python
@dataclass
class CashLedger:
    usd_to_cad_rate: float
    fee_cad: float
    balances: dict  # {account_number: {"CAD": float, "USD": float}}

    def cash(self, account_number, currency) -> float:
        return max(0.0, self.balances.get(account_number, {}).get(currency, 0.0))

    def total_buying_power(self, account_number, buy_currency, dlr_quotes=None) -> float:
        """Same-currency cash + what you could get by converting the other currency."""
        native = self.cash(account_number, buy_currency)
        source_currency = "CAD" if buy_currency == "USD" else "USD"
        convertible = cross_currency_buying_power(
            self.cash(account_number, source_currency),
            source_currency, self.usd_to_cad_rate, self.fee_cad, dlr_quotes
        )
        return native + convertible
```

**Key insight:** `total_buying_power` includes cross-currency cash. So if an account has $0 USD but $60,000 CAD, buying power for USD symbols is still ~US$44,000 (after conversion).

---

## Sizing Math Reference

### `shares_for_drift_gap` — How many shares to close a drift gap

```python
gap_cad = abs(drift_pct / 100.0) * total_value_cad
gap_native = gap_cad / usd_to_cad_rate if currency == "USD" else gap_cad
shares = floor(gap_native / price_native)
```

For sells: uses bid price (what you'd get). For buys: uses ask price (what you'd pay).

### `max_sellable_without_crossing_target` — Safety limit on sells

```python
per_share_drift_pct = (to_cad(price, currency, rate) / total_value_cad) * 100.0
return floor(drift_pct / per_share_drift_pct)
```

Example: If each VSP.TO share represents 0.0034% of drift, and drift is +2.3%, you can sell at most 676 shares before crossing zero.

---

## Trade Notes Reference

Each `TradeRecommendation` carries a `note` string displayed in the output. Here's every possible note and when it appears:

### Sell Notes

| Note | When | Example |
|------|------|---------|
| `Overweight sell` | Layer 1: Symbol drift exceeds threshold, selling to reduce allocation | SELL 691 VSP.TO — portfolio has 55.3% vs 53% target |
| `Displacement sell` | Cash raising: Selling an overweight holding in a specific account to fund a buy in that same account | Account holds XIGS.TO (overweight) and IVV (underweight) but no USD cash — sell XIGS.TO to fund IVV |

### Buy Notes

| Note | When | Example |
|------|------|---------|
| `Underweight buy` | Layer 2: Symbol drift below threshold, buying with same-currency cash | BUY 1495 ZMMK.TO — funded by CAD cash from VSP.TO sells |
| `Underweight buy (requires FX)` | Layer 2: Same as above but no same-currency cash — triggered cross-currency conversion | BUY 62 IVV — CAD converted to USD via Norbert's Gambit |
| `Leftover cash buy` | Layer 3: Account has leftover same-currency cash after starter trades, buying the most underweight symbol available | Account has $800 CAD remaining, buys 7 more ZMMK.TO |
| `Leftover cash buy (requires FX)` | Layer 3: Same as above but spending foreign-currency cash via conversion | Account has $200 CAD remaining, only holds USD symbols — convert and buy |
| `Best available buy` | Layer 3 fallback: No underweight symbols available in this account/currency, but cash exists — buy the least overweight symbol to minimize idle cash | All symbols at target, but $50 CAD sitting in account — buy 1 share of least-drifted holding |
| `Best available buy (requires FX)` | Layer 3 fallback with conversion: Same as above but funded by converting the other currency | Same situation but with foreign cash |

### How Notes Flow Through Netting

When `net_trades()` consolidates multiple trades for the same (symbol, account), the note from the **first** trade of each type (buy or sell) is preserved. So if a position was bought in Layer 2 ("Underweight buy") and again in Layer 3 ("Leftover cash buy"), the final netted trade keeps "Underweight buy" since it was the first buy note recorded.

---

## Full Execution Trace (Simplified)

Here's what happens for a typical run with VSP.TO overweight and IVV underweight:

```
1. RebalancePlanner.__post_init__()
   → initial_drifts = {VSP.TO: +2.3, IVV: -1.9, ZMMK.TO: -2.2, ...}
   → ledger = {acct_A: {CAD: $165, USD: $454}, ...}

2. build() → Round 1

3.   _sell_overweight_starters()
     → VSP.TO drift +2.3% > threshold 0.1%
     → shares_for_drift_gap(1M, 2.3%, $114.14, CAD, 1.36) = 201
     → allocate_sell: Account A has most VSP.TO + underweight alternatives
     → SELL 201 VSP.TO in acct_A → ledger[acct_A][CAD] += $22,942
     → Similar for XIGS.TO, XSH.TO

4.   _buy_underweight_starters()
     → ZMMK.TO drift -2.2% < -threshold
     → shares_for_drift_gap(1M, -2.2%, $49.85, CAD, 1.36) = 441
     → _eligible_buy_accounts: acct_A has CAD cash + holds ZMMK.TO
     → _buy_in_account: affordable = floor($22,942 / $49.85) = 460
     → quantity = min(441, 460) = 441
     → fund_buy: native CAD covers it → requires_fx=False
     → BUY 441 ZMMK.TO in acct_A

     → IVV drift -1.9% < -threshold
     → shares_for_drift_gap(1M, 1.9%, $726.93, USD, 1.36) = 19
     → _eligible_buy_accounts: acct_A has CAD (convertible) + holds IVV
     → _buy_in_account: total_buying_power includes cross-currency
     → fund_buy: native USD ($454) < cost → convert remainder from CAD
     → requires_fx=True
     → BUY 19 IVV in acct_A

5.   _deploy_residual_cash()
     → acct_A has $95 CAD remaining after trades
     → build_same_currency_buy → ZMMK.TO still underweight → buy 1 more
     → No more cash to deploy

6. build() → Round 2
     → _sell_overweight_starters: all drifts now < threshold → 0 changes
     → Loop exits

7. net_trades()
     → ZMMK.TO: 441 + 1 = 442 shares (netted into one BUY)
     → All others: single trades, pass through unchanged
```

---

## File Organization

```
rebalancer.py
├── net_trades()                    # Trade netting utility
├── calculate_trades()              # Public entry point
├── TradePlan                       # Projected state tracker
├── CashLedger                      # Per-account cash tracker
├── RebalancePlanner                # The planner engine
│   ├── __post_init__()             # Setup: drifts, holdings view, plan, ledger
│   ├── build()                     # Main loop (sell → buy → deploy × N rounds)
│   ├── _sell_overweight_starters() # Layer 1: identify + size + allocate sells
│   ├── _buy_underweight_starters() # Layer 2: identify + size + allocate buys
│   ├── _buy_symbol_toward_target() # Single-symbol buy logic
│   ├── _eligible_buy_accounts()    # Account sorting for buys
│   ├── _buy_in_account()           # Execute buy + fund it
│   ├── _raise_cash_in_account()    # Displacement sells for cash
│   ├── _deploy_residual_cash()     # Layer 3: spend remaining cash
│   ├── _account_buyable_candidates()  # Find buyable symbols in an account
│   ├── _build_cash_minimizing_same_currency_buy()   # Fallback: best available
│   └── _build_cash_minimizing_cross_currency_buy()  # Fallback: best available + FX
├── shares_for_drift_gap()          # Sizing: shares needed for drift
├── max_sellable_without_crossing() # Sizing: max safe sell
├── allocate_sell()                 # Multi-account sell distribution
├── find_accounts_for_symbol()      # Account lookup
├── get_position_quantity()         # Position lookup
├── _effective_qty()                # Position adjusted for planned trades
└── _has_underweight_alternatives() # Can proceeds be redeployed?
```
