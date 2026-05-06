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

---

## Real-World Scenarios

These scenarios demonstrate how the planner handles complex situations that arise in multi-account, multi-currency portfolios. They showcase the interplay between account constraints, currency conversion, displacement sells, and iterative convergence.

---

### Scenario 1: Large Cash Deposit into a Single-USD-Stock Account

**Setup:** You contribute $100,000 CAD to an RRSP that holds only one position — a US-listed ETF (e.g., QQQ). The rest of the portfolio across other accounts is roughly balanced.

**Why this is tricky:**
- Rule 5 says "only buy symbols that already exist in the account"
- The account's only buyable symbol is USD-denominated
- The cash is CAD
- Buying $100K worth of QQQ in this one account would massively overshoot the household target for QQQ

**What the planner does:**

```
Initial state:
  Portfolio total: ~$900K
  Account "Alice RRSP" (acct_A):
    QQQ: 140 shares @ US$512 = ~$97,500 (in CAD)
    Cash CAD: $100,000
    Cash USD: $0
  
  Household drift:
    QQQ: -0.2% (roughly on target before the deposit)
    → After deposit inflates total to ~$1M, QQQ is now -1.4% underweight
    → Everything else shifts slightly because the denominator grew

Round 1 — Layer 1 (Sells):
  Nothing is overweight enough to sell (the cash diluted everything)

Round 1 — Layer 2 (Buys):
  QQQ is underweight by -1.4%
  → shares_for_drift_gap($1M, -1.4%, US$512, "USD", 1.36) = 20 shares
  → _eligible_buy_accounts: acct_A holds QQQ
  → _buy_in_account: same-currency (USD) cash = $0
  → total_buying_power includes cross-currency:
      $100,000 CAD → ~US$73,000 (after Norbert's Gambit math)
  → affordable = floor($73,000 / $512) = 142 shares
  → quantity = min(20, 142) = 20 shares
  → fund_buy: native USD ($0) < cost → convert from CAD
  → BUY 20 QQQ in acct_A (requires FX)
  → Ledger deducts ~$14,000 CAD equivalent

Round 1 — Layer 3 (Residual Cash):
  acct_A still has ~$86,000 CAD after the QQQ buy
  
  build_same_currency_buy: No CAD-denominated symbols in this account → skip
  build_cross_currency_buy: Source=CAD, target=USD
    → QQQ is the only candidate in this account
    → But QQQ is no longer underweight after the 20-share buy!
    → Falls through to _build_cash_minimizing_cross_currency_buy
    → QQQ is the "best available" (only option in this account)
    → Buys as many QQQ shares as the remaining CAD can fund via FX
    → BUY ~120 QQQ in acct_A (best available buy, requires FX)
  
  After this: QQQ is now ~+11% OVERWEIGHT at the household level!
  But the cash is deployed. Rule 7 allows this.

Round 2 — Layer 1 (Sells):
  QQQ drift is now +11% — massively overweight
  → shares_for_drift_gap($1M, 11%, US$512, "USD", 1.36) = 158 shares
  → allocate_sell: Which accounts hold QQQ?
      acct_A has ~280 shares (original 140 + 140 just bought)
      acct_B has 45 shares
  → Sorting: acct_A has underweight alternatives? No (only QQQ)
              acct_B has underweight alternatives? Yes (holds VUN.TO, XEF.TO)
  → acct_B ranks first! Sell 45 from acct_B
  → Still need 113 more → sell 113 from acct_A
  → SELL 45 QQQ in acct_B, SELL 113 QQQ in acct_A

Round 2 — Layer 2 (Buys):
  acct_B now has ~US$23,000 from the QQQ sale
  VUN.TO is underweight → BUY VUN.TO in acct_B
  XEF.TO is underweight → BUY XEF.TO in acct_B
  ... proceeds redeployed into underweight positions

Trade netting:
  acct_A: bought 140 QQQ then sold 113 = NET BUY 27 QQQ
  acct_B: sold 45 QQQ, bought VUN.TO + XEF.TO

Final result:
  The $100K CAD was effectively:
  - Partially converted to USD and invested in QQQ (in acct_A)
  - Partially "transferred" by selling QQQ from acct_B
  - Those proceeds rebalanced other positions in acct_B
  - Net effect: portfolio drifts back toward target across the household
```

**Key insight:** The planner uses the constrained account (only holds QQQ) as a "pass-through" — it buys what it can, overshoots, then the overweight sell in Round 2 liberates cash in other accounts that have better options. Trade netting ensures the final recommendation is clean.

---

### Scenario 2: Displacement Sell Creates Cross-Account Rebalancing

**Setup:** VUN.TO is slightly overweight (+1.5%) and QQQ is underweight (-1.8%). Account "Bob Margin" holds both. The planner sells VUN.TO in Bob's account and buys QQQ there — but this makes VUN.TO go underweight across the household. VUN.TO is then bought in a different account.

**What the planner does:**

```
Initial state:
  Portfolio total: ~$850K
  
  Account "Bob Margin" (acct_B):
    VUN.TO: 2,800 shares @ $62.18 = $174,104
    QQQ: 50 shares @ US$512 = ~$34,816 (in CAD)
    XEF.TO: 1,200 shares @ $38.42 = $46,104
    Cash CAD: $200, Cash USD: $0
  
  Account "Alice TFSA" (acct_A):
    VUN.TO: 1,500 shares @ $62.18 = $93,270
    ZAG.TO: 8,000 shares @ $10.85 = $86,800
    Cash CAD: $150, Cash USD: $0
  
  Drift: VUN.TO +1.5%, QQQ -1.8%, XEF.TO +0.1%, ZAG.TO -0.3%

Round 1 — Layer 1 (Sells):
  VUN.TO is overweight (+1.5% > threshold)
  → shares_for_drift_gap($850K, 1.5%, $62.18, "CAD", 1.36) = 205 shares
  → allocate_sell: acct_B has 2,800 VUN.TO + underweight alternatives (QQQ)
                   acct_A has 1,500 VUN.TO + underweight alternatives (ZAG.TO)
  → acct_B ranks first (larger position + has alternatives)
  → SELL 205 VUN.TO in acct_B → ledger[acct_B][CAD] += $12,747

Round 1 — Layer 2 (Buys):
  QQQ is underweight (-1.8%)
  → shares_for_drift_gap($850K, -1.8%, US$512, "USD", 1.36) = 22 shares
  → _eligible_buy_accounts: acct_B holds QQQ
  → _buy_in_account: buying_power = $0 USD + convertible ~$9,500 from CAD
  → affordable = floor($9,500 / $512) = 18 shares
  → But wait — that's not enough. _raise_cash_in_account triggered:
      → Looks for overweight same-currency holdings to sell
      → XEF.TO is +0.1% overweight, but barely
      → No meaningful displacement candidates
  → BUY 18 QQQ in acct_B (requires FX)
  → Remaining 4 shares unfilled this round

  After this buy, recalculate drifts:
  VUN.TO is now -0.7% (selling 205 shares pushed it underweight!)
  QQQ is now -0.3% (closer to target but not done)

  VUN.TO now underweight → eligible for buying
  → _eligible_buy_accounts: acct_A holds VUN.TO + has CAD cash? Only $150...
  → Not enough to buy — skipped this round

Round 1 — Layer 3 (Residual Cash):
  acct_B has small leftover CAD from VUN.TO sale minus QQQ conversion
  → build_same_currency_buy: VUN.TO is underweight, buy 1-2 shares back
  → Interesting! We just sold VUN.TO and now we're buying some back.
  → But trade netting will handle this: 205 sold - 2 bought back = net 203 sold

Round 2 — Layer 1 (Sells):
  Nothing overweight enough anymore

Round 2 — Layer 2 (Buys):
  VUN.TO still slightly underweight (-0.5%)
  → shares_for_drift_gap($850K, -0.5%, $62.18, CAD) = 68 shares
  → _eligible_buy_accounts: acct_A holds VUN.TO, has $150 CAD (not enough)
      acct_B holds VUN.TO, has some leftover CAD
  → Between both accounts, buy what's affordable
  → BUY 2 VUN.TO in acct_B (leftover cash)
  → acct_A can't contribute meaningfully

Trade netting:
  acct_B VUN.TO: SOLD 205 + BOUGHT 4 = NET SELL 201 VUN.TO
  acct_B QQQ: NET BUY 18 QQQ
  Everything else: pass-through

Final trades presented to user:
  SELL 201 VUN.TO in Bob Margin
  BUY 18 QQQ in Bob Margin (requires currency conversion)
```

**Key insight:** The sell of VUN.TO funded the QQQ buy in the same account. VUN.TO went from overweight to slightly underweight — but because cash was limited in accounts that hold VUN.TO, the planner couldn't fully buy it back elsewhere. The trade netting collapses the "sold 205, bought back 4" into a clean "sell 201." The user never sees the intermediate back-and-forth.

---

### Scenario 3: Multi-Account Cascade — One Sell Triggers a Chain Reaction

**Setup:** A portfolio with three accounts where a single overweight position triggers trades across all accounts through the displacement mechanism.

**What the planner does:**

```
Initial state:
  Portfolio total: ~$750K
  
  Account "Alice RRSP" (acct_A):  
    XEF.TO: 3,000 shares @ $38.42 = $115,260
    VUN.TO: 500 shares @ $62.18 = $31,090
    Cash CAD: $50
  
  Account "Bob TFSA" (acct_B):
    VUN.TO: 2,000 shares @ $62.18 = $124,360
    ZAG.TO: 5,000 shares @ $10.85 = $54,250
    Cash CAD: $80
  
  Account "Alice TFSA" (acct_C):
    QQQ: 120 shares @ US$512 = ~$83,558
    ZAG.TO: 6,000 shares @ $10.85 = $65,100
    XEF.TO: 1,500 shares @ $38.42 = $57,630
    Cash CAD: $30, Cash USD: $200
  
  Drift:
    XEF.TO: +3.2% (significantly overweight)
    VUN.TO: +0.8%
    QQQ: -1.9% (underweight)
    ZAG.TO: -2.1% (most underweight)

Round 1 — Layer 1 (Sells):
  XEF.TO +3.2% overweight
  → 625 shares to sell
  → allocate_sell priority:
      acct_A: 3,000 shares, has underweight VUN.TO? No (VUN.TO is +0.8%)
      acct_C: 1,500 shares, has underweight QQQ? Yes! (-1.9%)
  → acct_C ranks first (has underweight alternatives)
  → SELL 625 XEF.TO from acct_C → $24,012 CAD in acct_C

  VUN.TO +0.8% overweight
  → 96 shares to sell
  → SELL 96 VUN.TO from acct_B (largest position)
  → $5,969 CAD in acct_B

Round 1 — Layer 2 (Buys):
  ZAG.TO is -2.1% underweight
  → 1,452 shares needed
  → _eligible_buy_accounts: acct_B has ZAG.TO + $6,049 CAD
                             acct_C has ZAG.TO + $24,042 CAD
  → acct_C has more cash → fills first
  → BUY 1,452 ZAG.TO split across acct_C and acct_B

  QQQ is -1.9% underweight
  → 20 shares needed
  → _eligible_buy_accounts: acct_C holds QQQ
  → _buy_in_account: remaining CAD in acct_C after ZAG.TO buys?
  → If not enough: _raise_cash_in_account
      → Candidates in acct_C: XEF.TO is still slightly overweight after sell
      → DISPLACEMENT SELL: sell more XEF.TO from acct_C to fund QQQ
  → Conversion CAD → USD via Norbert's Gambit
  → BUY 20 QQQ in acct_C (requires FX)

Round 1 — Layer 3 (Residual Cash):
  Tiny amounts left in each account
  → Various 1-2 share "leftover cash buy" trades

The cascade:
  1. XEF.TO sell in acct_C → cash freed
  2. Cash funds ZAG.TO buy in acct_C AND acct_B
  3. VUN.TO sell in acct_B → cash for more ZAG.TO in acct_B
  4. Displacement sell of XEF.TO in acct_C → funds QQQ buy (with FX)
  5. A single overweight triggered useful trades in all three accounts
```

**Key insight:** The `_has_underweight_alternatives` sort in `allocate_sell` is what makes this work elegantly. By selling XEF.TO from the account that *also holds underweight QQQ*, the proceeds can be immediately redeployed within that same account — even though QQQ requires a currency conversion. The planner routes cash where it can do the most good.

---

### Scenario 4: The "Best Available" Fallback — No Good Options, But Cash Must Move

**Setup:** An account holds only two CAD symbols. Both are at or above target. But the account received $2,000 CAD from a dividend or prior sell. There's nothing underweight to buy — but leaving cash idle violates Rule 4.

```
Account "Bob RRSP":
  VUN.TO: 800 shares @ $62.18, drift +0.3%
  XBB.TO: 2,000 shares @ $28.94, drift +0.1%
  Cash CAD: $2,000

Neither VUN.TO nor XBB.TO is underweight. Normal underweight buys produce nothing.

Fallback chain:
  1. build_same_currency_buy → no underweight candidates → returns None
  2. _build_cash_minimizing_same_currency_buy kicks in
     → _account_buyable_candidates(underweight_only=False)
     → Returns ALL buyable symbols sorted by drift (lowest first)
     → XBB.TO at +0.1% ranks before VUN.TO at +0.3%
     → BUY 69 XBB.TO @ $28.94 = $1,996.86 ("Best available buy")

Result: Cash is deployed into the least-overweight option.
The drift impact is minimal (+0.1% → +0.3%) but cash isn't stranded.
```

**Key insight:** The "best available" fallback exists because idle cash has a real cost in a rebalancing portfolio — it creates drift in the CAD/USD cash allocation itself. The fallback picks the *least bad* option rather than the *best* option.

---

### Scenario 5: Trade Netting Saves You From Silly-Looking Recommendations

**Setup:** The planner decides to sell VUN.TO from an account in Layer 1 (overweight sell), then in Layer 3 buys back some VUN.TO in the same account with leftover cash. Without netting, the user would see "SELL 200" and "BUY 12" for the same symbol in the same account.

```
Raw trades generated by planner:
  Round 1, Layer 1: SELL 200 VUN.TO in acct_A @ $62.18 ("Overweight sell")
  Round 1, Layer 3: BUY 12 VUN.TO in acct_A @ $62.18 ("Leftover cash buy")

After net_trades():
  key = ("VUN.TO", "acct_A")
  total_buy_qty = 12
  total_sell_qty = 200
  net_quantity = 12 - 200 = -188
  → Final: SELL 188 VUN.TO in acct_A ("Overweight sell")

The user sees one clean trade, not two contradictory ones.
```

**Why this happens:** Selling 200 shares frees up more cash than the subsequent buy rounds can fully spend on other symbols (due to whole-share rounding). The leftover trickles back into VUN.TO because it's the best remaining candidate. The netting makes this invisible.

---

### Scenario 6: Cross-Currency Sweep — Don't Leave Foreign Cash Stranded

**Setup:** After all trades are planned, an account holds only USD positions but has $500 CAD sitting in it. This CAD can never be naturally deployed because there are no CAD symbols to buy.

```
Account "Alice TFSA":
  QQQ: 80 shares (USD)
  VFV: 200 shares (USD)
  Cash CAD: $500
  Cash USD: $12

Post-trade planning (fx_conversions.py):
  All positions are USD → this is a "single-currency account"
  CAD cash ($500) > fee ($10.49) → eligible for sweep
  
  Sweep logic:
    cad_for_shares = $500 - $10.49 = $489.51
    sweep_shares = floor($489.51 / $13.79) = 35 DLR.TO shares
    → Convert: Buy 35 DLR.TO @ $13.79 = $482.65 CAD
    → Sell 35 DLR.U.TO @ $10.15 = US$355.25
  
  Conversion recommendation added to the Norbert's Gambit table.
  If the account already needed a CAD→USD conversion for a trade,
  the sweep shares are ADDED to the existing conversion (same journal entry).
```

**Key insight:** The sweep logic in `fx_conversions.py` runs *after* the rebalancer. It detects "orphaned" foreign cash that can never be productively used and folds it into the conversion plan. This prevents the slow accumulation of small unusable cash balances over time.

---

### How These Scenarios Interact With the Rules

| Scenario | Primary Rules Exercised |
|----------|------------------------|
| 1. Large cash deposit (single USD stock) | Rule 5 (only buy existing), Rule 7 (constrained deploy + global cleanup), Rule 4 (minimize cash) |
| 2. Displacement sell cascade | Rule 2 (sell overweight/buy underweight), Rule 5 (account constraints), Rule 8 (netting avoids churn) |
| 3. Multi-account chain reaction | Rule 1 (household-level drift), Rule 5 (per-account buyable universe), Rule 6 (same-currency first) |
| 4. Best available fallback | Rule 4 (minimize cash), Rule 5 (only existing holdings) |
| 5. Trade netting | Rule 8 (avoid wasteful churn) |
| 6. Cross-currency sweep | Rule 4 (minimize cash), Rule 6 (prefer same-currency, then convert) |

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
