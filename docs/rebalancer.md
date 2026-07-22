# Rebalancer Algorithm Deep Dive

How `src/rebalancer.py` decides what to buy, sell, and where — following the actual code path.

---

## Rebalancing Rules

The planner is built around these rules (defined in the project README). They're referenced throughout the scenarios below as "Rule N":

1. **Treat all accounts as one household portfolio** — Drift is measured at the total-portfolio level, not per account.
2. **Sell overweight positions and buy underweight positions** — The planner starts with symbols whose drift is materially away from target.
3. **Use a drift threshold to avoid tiny starter trades** — The configured `drift_trade_threshold_pct` suppresses trades for symbols that are only slightly off target.
4. **Minimize free cash whenever practical** — Useful account-level cash is deployed even when no symbol is far enough from target to start a normal rebalance. Idle cash flows to where it is most useful: to a real underweight when one exists, otherwise (best-available) to the holding with the most cascade potential. Crossing the currency boundary for a best-available buy is only worth a conversion when the destination has cascade potential; genuinely stranded foreign cash is instead handled by the post-rebalance conversion sweep.
5. **Only buy symbols that already exist in the account** — An account's current holdings define its buyable universe.
6. **Prefer same-currency deployment before cross-currency deployment** — Same-currency buys come before CAD/USD conversion, since a same-currency option of equal value avoids a Norbert's Gambit fee. Cross-currency funding is used when it closes a real underweight, or when a best-available buy on the other side has strictly higher cascade potential than anything same-currency.
7. **Allow account-constrained cash deployment, then clean up globally** — If cash lands in an account with limited options, deploy it there first. If that creates excess exposure, sell the excess from another account where proceeds can fund underweight buys.
8. **Avoid obviously wasteful churn** — Don't create trade patterns that undo each other without improving drift or reducing idle cash.
9. **Use whole-share, real-side pricing** — Sells use bid pricing. Buys use ask pricing. Whole shares only.
10. **Treat unknown symbols as implicit 0% targets** — Holdings not in the target map are eligible to be sold.
11. **Respect transient/excluded symbols** — Symbols like `DLR.TO` / `DLR.U.TO` can be temporarily excluded while a Norbert's Gambit is in flight.

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

        cash_changes = self._deploy_residual_cash()

        if starter_changes == 0 and cash_changes == 0:
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
current_drifts = self.plan.drifts()
productive = self._productive_accounts_for_sell(symbol, current_drifts)
sell_trades = allocate_sell(
    symbol, shares, bid_price_native, currency,
    self.portfolio.accounts,
    effective_drift=current_drifts,
    transient_symbols=self.hidden_symbols,
    position_deltas=self.plan.position_deltas(),
    productive_accounts=productive,
)
```

**What `_productive_accounts_for_sell` determines:** An account can productively use sell proceeds if it has:
1. An underweight alternative (direct rebalancing) — *any* position below target (drift < 0), matching what the residual-cash deployment layer will actually buy. This is intentionally looser than `drift_trade_threshold_pct`: that threshold suppresses tiny *starter* trades, but once an overweight sell has been triggered its proceeds should be usable against any underweight holding rather than left stranded. OR
2. An alternative symbol with greater cascade potential than the sell symbol (routes value out through a better conduit — buying it may overshoot household allocation, triggering a sell from a *different* account where proceeds fund underweight buys)

If neither condition holds, selling from that account would just generate cash that buys the same symbol back during residual deployment — leaking bid/ask spread for no benefit.

Inside `allocate_sell`:

```python
holders = find_accounts_for_symbol(symbol, accounts)

# Exclude accounts that can't productively use the proceeds
if productive_accounts is not None:
    holders = [a for a in holders if a.number in productive_accounts]

# Sort by: (1) has underweight alternatives? (2) position size
holders.sort(
    key=lambda a: (
        1 if _has_underweight_alternatives(a, symbol, ...) else 0,
        effective_qty(a, symbol, position_deltas),
    ),
    reverse=True,
)
```

**What `_has_underweight_alternatives` checks:** Can this account's sell proceeds be immediately redeployed? It scans the account's other positions for anything below target — *any* negative drift qualifies (`drift < 0`), matching what residual-cash deployment will actually buy.

**What `effective_qty` does:** Returns the position size *adjusted for trades already planned this round*. Prevents over-selling when multiple rounds target the same account.

```python
def effective_qty(account, symbol, position_deltas):
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
    self._raise_cash_in_account(
        acct, symbol, currency,
        target_native=remaining_shares * ask_price_native,
        min_useful_native=ask_price_native,
    )
    buying_power = self.ledger.total_buying_power(...)  # Recalculate

affordable = int(math.floor(buying_power / ask_price_native))
quantity = min(remaining_shares, affordable)

# Fund the purchase (returns True if cross-currency conversion was needed)
converted = self.ledger.fund_buy(acct.number, currency, cost_native, dlr_quotes=...)

self.plan.add_trade(TradeRecommendation(
    ...,
    note="Underweight buy (FX)" if converted else "Underweight buy",
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

## Cash Raising: Funding Sells (`_raise_cash_in_account`)

When an account wants to buy IVV (USD) but has no USD and no CAD to convert, the planner can sell overweight *same-currency* holdings in that account:

```python
def _raise_cash_in_account(self, acct, buy_symbol, currency,
                           target_native, min_useful_native=None):
    if min_useful_native is None:
        min_useful_native = target_native
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

Two parameters, not one: `target_native` is how much cash we'd *like* to raise (enough for all `remaining_shares`), while `min_useful_native` is the floor below which raising cash is pointless (one share of the buy symbol). Callers pass `target_native=remaining_shares * ask_price`, `min_useful_native=ask_price`.

**`max_sellable_without_crossing_target`** — this is the safety valve:

```python
def max_sellable_without_crossing_target(total_value_cad, drift_pct, price, currency, rate):
    per_share_drift_pct = (to_cad(price, currency, rate) / total_value_cad) * 100.0
    return int(math.floor((drift_pct + 1e-9) / per_share_drift_pct))
```

It calculates: "each share I sell reduces drift by X%. I have Y% of positive drift. So I can sell at most Y/X shares before going negative." (The `+ 1e-9` nudge absorbs floating-point error at exact share boundaries — see [the floating-point nudge](#fp-nudge).)

Then it runs a **dry-run first**: it simulates selling the most overweight candidates until the target is met, and only commits the "Funding sell" trades if the simulated proceeds reach `min_useful_native`. This prevents orphaned sells that raise cash but not enough to buy even one share of the target:

```python
candidates.sort(key=lambda item: item[0], reverse=True)  # Most overweight first
simulated_cash = current_cash
planned_sells = []
for _drift_pct, symbol, bid_price, max_sellable in candidates:
    shortfall = target_native - simulated_cash
    if shortfall <= 0:
        break  # Simulated enough
    sell_qty = min(max_sellable, ceil(shortfall / bid_price))
    simulated_cash += bid_price * sell_qty
    planned_sells.append((symbol, sell_qty, bid_price, ...))

# Only commit if the dry-run cleared the minimum-useful floor
if simulated_cash < min_useful_native:
    return
# ... commit each planned sell as a "Funding sell" trade
```

---

## Layer 3: Residual Cash Deployment (`_deploy_residual_cash`)

Cash can remain in accounts from rounding, pre-existing balances, dividends, or partial fills. This layer minimizes idle cash and runs even when no starter sell or buy was needed.

### The two-layer approach

```python
def _deploy_residual_cash(self) -> int:
    while True:
        made_trade = False
        projected_drifts = self.plan.drifts()

        for acct in self.portfolio.accounts:
            # Layer 1: underweight buys — same-currency first, then cross-currency.
            for source_currency in ("CAD", "USD"):
                cross = "USD" if source_currency == "CAD" else "CAD"
                for buy_currency in (source_currency, cross):
                    while True:
                        trade = build_deploy_cash_underweight(
                            ..., source_currency=source_currency,
                            buy_currency=buy_currency, ...
                        )
                        if trade is None:
                            break
                        self.plan.add_trade(trade); made_trade = True

            # Layer 2: best-available deployment for leftover cash. One call per
            # source currency; it compares same- vs cross-currency internally.
            for source_currency in ("CAD", "USD"):
                while True:
                    trade = self._build_deploy_cash_any(
                        acct, source_currency, projected_drifts,
                    )
                    if trade is None:
                        break
                    self.plan.add_trade(trade); made_trade = True

        if not made_trade:
            break  # No more cash to deploy anywhere
```

### The two layers

1. **`build_deploy_cash_underweight`** (from `cash_deploy.py`) — Buy the most underweight symbol in this account that matches the buy currency. Iterates through candidates if the top pick is too expensive. Caps at the number of shares needed to close the gap. Handles both same-currency (`source_currency == buy_currency`) and cross-currency cases. Cross-currency underweight buys are always allowed — closing a real drift gap justifies the conversion fee regardless of cascade.

2. **`_build_deploy_cash_any`** — Best-available deployment once nothing is underweight. For a given source currency it takes the top (highest-cascade) buyable candidate on *each* side and deploys into whichever has the higher cascade score, preferring the same-currency buy on ties (no conversion fee). Crossing the currency boundary here is gated on cascade potential: the cross-currency buy only fires when its cascade score is **strictly positive** — otherwise a conversion would pay a fee to park cash in an at-target holding with no downstream benefit. Genuinely stranded foreign cash (no cascade destination) is left for the post-rebalance conversion sweep (see `fx_conversions.py`), which moves it into the account's home currency.

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

### The best-available candidate list (`_account_buyable_candidates`)

When `underweight_only=False`, this returns ALL buyable symbols in the currency (any drift, but only those with a target > 0), sorted by **cascade score** — the symbol most likely to unlock a productive sell elsewhere wins, with lowest drift as the tiebreaker:

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

    if underweight_only:
        candidates.sort(key=lambda item: item[0])  # Most underweight first
    else:
        # Highest cascade score first, lowest drift as tiebreaker
        candidates.sort(key=lambda item: (-self._cascade_score(item[1], acct.number, drifts), item[0]))
    return candidates
```

`_build_deploy_cash_any` calls this for both the source currency and the other currency, takes the top (highest-cascade) candidate from each via `_top_buyable_candidate`, and deploys into whichever has the higher cascade score — see [Layer 3](#layer-3-residual-cash-deployment-_deploy_residual_cash).

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

### Deep Dive: Portfolio Projection (`simulate_rebalance`)

The `TradePlan` needs to answer "if we executed all these trades, what would the portfolio look like?" This is how `simulate_rebalance` works:

#### The Core Logic

```python
def simulate_rebalance(portfolio, trades, targets, usd_to_cad_rate, hidden_symbols):
    # Start with current portfolio values
    projected_holdings_value_cad = {symbol: holding.value_cad for ...}
    projected_cash_cad = portfolio.cash_cad_total
    projected_cash_usd = portfolio.cash_usd_total

    for trade in trades:
        trade_value_cad = price_in_cad × quantity

        if trade.action == "BUY":
            projected_holdings_value_cad[symbol] += trade_value_cad
            # Deduct cash based on funding source
            if trade.requires_fx:
                # Deducted from the OTHER currency
                ...
            else:
                # Deducted from same currency
                ...
        elif trade.action == "SELL":
            projected_holdings_value_cad[symbol] -= trade_value_cad
            # Credit cash in native currency
            ...
```

#### The Negative Cash Correction

A subtle but important detail — after applying all trades, cash can go negative due to rounding and estimation differences:

```python
if projected_cash_cad < 0:
    projected_cash_usd += projected_cash_cad / usd_to_cad_rate
    projected_cash_cad = 0
if projected_cash_usd < 0:
    projected_cash_cad += projected_cash_usd * usd_to_cad_rate
    projected_cash_usd = 0
```

This accounts for the implicit cross-currency flows that happen during FX-funded buys. The projection treats negative cash as "will be covered by the other currency," which matches what actually happens during Norbert's Gambit.

#### Why This Matters for the Planner

`TradePlan.drifts()` calls `simulate_rebalance` every time the planner needs to check "where are we now?" after previous trades. The accuracy of this projection determines whether the planner makes good decisions in subsequent rounds. If the projection were wrong, the planner might:
- Keep selling an already-underweight symbol
- Not buy enough of a symbol that's still underweight
- Create oscillating buy/sell cycles that never converge

### Deep Dive: The Cache Invalidation Pattern

The `TradePlan` uses a deliberate invalidation-on-mutation pattern that balances performance with correctness:

```python
def add_trade(self, trade):
    self.trades.append(trade)
    self._invalidate()           # Blow away all cached projections

def _invalidate(self):
    self._netted_cache = None    # Trade netting must be recomputed
    self._snapshot_cache = None  # Portfolio projection must be recomputed
```

#### Why Not Incremental Updates?

You might think "just apply the delta to the cached snapshot." But:

1. **Netting changes the picture:** Adding a BUY trade might cancel a previous SELL, changing the net quantity and thus the projected value in a non-obvious way.
2. **Drift is percentage-based:** Adding a trade changes the total portfolio value (cash moves), which changes ALL drift percentages — not just the symbol you traded.
3. **Cross-currency effects:** An FX-funded buy changes both CAD and USD cash projections, which ripple into CAD/USD drift calculations.

Full recomputation from netted trades is the only way to get a consistent picture. The caching ensures this only happens when the trade list actually changes.

#### Call Frequency

In a typical run with 5-8 symbols and 3 accounts:
- `drifts()` is called ~20-30 times per round
- `add_trade()` is called ~8-12 times per round
- So caching saves ~10-20 redundant `simulate_rebalance` calls per round

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
shares = floor(gap_native / price_native + 1e-9)
```

For sells: uses bid price (what you'd get). For buys: uses ask price (what you'd pay).

<a id="fp-nudge"></a>**The `+ 1e-9` nudge.** The drift→shares round-trip (dollars → drift % → dollars → ÷ FX rate) accumulates binary floating-point error that can land a whole-share result a hair below an integer (e.g. `19.9999999998`). Without the nudge, `floor` drops it and under-sells by one share — notably leaving a 1-share sliver when fully liquidating a target-0 position, which would keep that position (and its currency) alive when it should be fully cleared. The same nudge appears in `max_sellable_without_crossing_target` below.

### `max_sellable_without_crossing_target` — Safety limit on sells

```python
per_share_drift_pct = (to_cad(price, currency, rate) / total_value_cad) * 100.0
return floor((drift_pct + 1e-9) / per_share_drift_pct)
```

Example: If each VSP.TO share represents 0.0034% of drift, and drift is +2.3%, you can sell at most 676 shares before crossing zero. (The `+ 1e-9` nudge is the same floating-point guard described [above](#fp-nudge).)

---

## Trade Notes Reference

Each `TradeRecommendation` carries a `note` string displayed in the output. Here's every possible note and when it appears:

### Sell Notes

| Note | When | Example |
|------|------|---------|
| `Overweight sell` | Layer 1: Symbol drift exceeds threshold, selling to reduce allocation | SELL 691 VSP.TO — portfolio has 55.3% vs 53% target |
| `Funding sell` | Cash raising: Selling an overweight holding in a specific account to fund a buy in that same account | Account holds XIGS.TO (overweight) and IVV (underweight) but no USD cash — sell XIGS.TO to fund IVV |

### Buy Notes

Notes are terse because the display column is narrow. The `(FX)` suffix marks a buy funded by a Norbert's Gambit conversion; the `Action` column already shows `BUY`, so the word "buy" is omitted. The two `Deploy cash` notes come from Layer 3 (residual cash deployment) and share a family name — the qualifier says whether the buy was restricted to underweight holdings or free to pick any holding by cascade potential.

| Note | When | Example |
|------|------|---------|
| `Underweight buy` | Starter buy (Layer 2): symbol materially underweight, funded with same-currency cash | BUY 1495 ZMMK.TO — funded by CAD cash from VSP.TO sells |
| `Underweight buy (FX)` | Starter buy: same as above but no same-currency cash — triggered a cross-currency conversion | BUY 62 IVV — CAD converted to USD via Norbert's Gambit |
| `Deploy cash (underweight)` | Residual (Layer 3, `build_deploy_cash_underweight`): leftover cash buys an underweight holding (any negative drift) | Account has $800 CAD remaining, buys 7 more ZMMK.TO |
| `Deploy cash (underweight) (FX)` | Same as above but funded cross-currency to close a real underweight | Account has $200 CAD remaining, only holds USD underweight symbols — convert and buy |
| `Deploy cash (any)` | Residual (Layer 3, `_build_deploy_cash_any`): nothing underweight, so leftover cash buys the highest-cascade holding to minimize idle cash | All symbols at/above target, but $50 CAD sits idle — buy 1 share of the highest-cascade holding |
| `Deploy cash (any) (FX)` | Same as above but crossing currencies, because the other side's best holding has strictly higher cascade potential | USD cash sits in a CAD-only-underweight household — convert and buy the high-cascade CAD holding |

### How Notes Flow Through Netting

When `net_trades()` consolidates multiple trades for the same (symbol, account), the note from the **first** trade of each type (buy or sell) is preserved. So if a position was bought as a starter ("Underweight buy") and again in Layer 3 ("Deploy cash (underweight)"), the final netted trade keeps "Underweight buy" since it was the first buy note recorded.

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
     → build_deploy_cash_underweight (same-currency) → ZMMK.TO still underweight → buy 1 more
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

These scenarios demonstrate how the planner handles complex situations that arise in multi-account, multi-currency portfolios. They showcase the interplay between account constraints, currency conversion, funding sells, and iterative convergence.

---

### Scenario 1: Large Cash Deposit into a Single-USD-Stock Account

**Setup:** You contribute $100,000 CAD to an RRSP that holds only one position — a US-listed ETF (e.g., QQQ). The rest of the portfolio across other accounts is roughly balanced.

**Why this is tricky:**
- Rule 5 ("Only buy symbols that already exist in the account") constrains the buyable universe
- The account's only buyable symbol is USD-denominated
- The cash is CAD
- Buying $100K worth of QQQ in this one account would massively overshoot the household target for QQQ
- But since QQQ is the *only* option in this account, the cash is effectively trapped there

**What the planner does:**

```
Initial state:
  Portfolio total: ~$900K
  Account "Alice RRSP" (acct_A):
    QQQ: 140 shares @ US$512 = ~$97,500 (in CAD)
    Cash CAD: $100,000
    Cash USD: $0
  
  Account "Bob Margin" (acct_B):
    QQQ: 45 shares @ US$512 = ~$31,334 (in CAD)
    VUN.TO: 2,000 shares @ $62.18
    XEF.TO: 1,500 shares @ $38.42
    Cash CAD: $200, Cash USD: $0

  Household drift:
    QQQ: -0.2% (roughly on target before the deposit)
    → After deposit inflates total to ~$1M, QQQ is now -1.4% underweight
    → Everything else shifts slightly because the denominator grew

Round 1 — Layer 1 (Sells):
  Nothing is overweight enough to sell (the cash diluted everything)

Round 1 — Layer 2 (Buys):
  QQQ is underweight by -1.4%
  → shares_for_drift_gap($1M, -1.4%, US$512, "USD", 1.36) ≈ 23 shares
  → _eligible_buy_accounts: acct_A holds QQQ
  → _buy_in_account: same-currency (USD) cash = $0
  → total_buying_power includes cross-currency:
      $100,000 CAD → ~US$73,000 (after Norbert's Gambit math)
  → affordable = floor($73,000 / $512) = 142 shares
  → quantity = min(23, 142) = 23 shares   ← capped at drift gap!
  → fund_buy: native USD ($0) < cost → convert from CAD
  → BUY 23 QQQ in acct_A (FX)   ← closes the real underweight only
  → Ledger deducts ~$16,000 CAD equivalent

Round 1 — Layer 3 (Residual Cash):
  acct_A still has ~$86,000 CAD after the QQQ buy

  build_deploy_cash_underweight(source=CAD, buy=CAD): No CAD-denominated symbols → skip
  build_deploy_cash_underweight(source=CAD, buy=USD):
    → QQQ is the only candidate in this account
    → QQQ is no longer underweight after the 23-share buy → skip
  → acct_A's ~$86,000 CAD is left in place for this run.

  The post-rebalance sweep handles the leftover CAD (see Scenario 6).
  acct_A's only position (QQQ) is USD, so USD is its single "home" currency;
  the sweep converts the ~$86,000 CAD into USD (Buy DLR.TO → journal →
  Sell DLR.U.TO). Next run, that cash is USD and deploys into QQQ
  same-currency.

Trade netting (final output):
  acct_A: NET BUY 23 QQQ  (+ a CAD→USD conversion from the sweep)
  acct_B: small same-currency underweight top-ups

Final result:
  - QQQ's genuine -1.4% underweight is closed by the 23-share buy.
  - The bulk of the deposit ($86K) is parked as USD cash by the sweep,
    ready to buy QQQ on the next run once it is same-currency.
  - No sell/re-buy oscillation, because the planner never force-bought QQQ
    past target in the first place.
```

**Key insight:** Cash deposited into a single-symbol account is deployed in two steps. Same-round, the planner buys only the genuine underweight gap — it will not burn an FX conversion to push a holding *past* target just to empty the account. The remaining foreign-relative cash is moved to the account's home currency by the post-rebalance sweep, so the following run can deploy it same-currency.

---

### Scenario 2: `allocate_sell` Splits a Sell Across Accounts Based on Redeployment Potential

**Setup:** VUN.TO is significantly overweight (+2.5%). QQQ is underweight (-2.0%). ZAG.TO is slightly underweight (-0.8%, below the 1% starter threshold). Account "Bob Margin" holds VUN.TO (small position) plus QQQ — the only account with QQQ. Account "Alice RRSP" holds VUN.TO (large position) plus ZAG.TO and XEF.TO but nothing that's materially underweight beyond threshold.

**Why this is interesting:**
- `allocate_sell` prefers accounts with underweight alternatives — so Account B (which holds underweight QQQ) gets sold first, even though it has fewer shares
- Account B runs out of VUN.TO before the sell is complete, forcing the remainder to spill to Account A
- Each account independently deploys its own proceeds into different symbols: QQQ in B, ZAG.TO in A
- There's no causal chain between accounts — the household drift signal determines where to sell, then each account acts in isolation with its own cash

**What the planner does:**

```
Initial state:
  Portfolio total: ~$1M
  drift_trade_threshold_pct: 1.0%
  
  Account "Alice RRSP" (acct_A):
    VUN.TO: 3,800 shares @ $62.18 = $236,284
    ZAG.TO: 25,000 shares @ $10.85 = $271,250
    XEF.TO: 3,500 shares @ $38.42 = $134,470
    Cash CAD: $300, Cash USD: $0
  
  Account "Bob Margin" (acct_B):
    VUN.TO: 200 shares @ $62.18 = $12,436
    QQQ: 75 shares @ US$512 ≈ $52,224 (in CAD)
    Cash CAD: $200, Cash USD: $0
  
  Drift: VUN.TO +2.5%, QQQ -2.0%, ZAG.TO -0.8%, XEF.TO +0.3%

Round 1 — Layer 1 (Sells):
  VUN.TO is overweight (+2.5% > 1.0% threshold)
  → shares_for_drift_gap($1M, 2.5%, $62.18, "CAD", 1.36) = 402 shares
  → allocate_sell: Which accounts hold VUN.TO?
      acct_B: 200 shares. _has_underweight_alternatives?
        → QQQ drift -2.0% < -1.0% threshold → YES
        → Sort score: (1, 200)
      acct_A: 3,800 shares. _has_underweight_alternatives?
        → ZAG.TO drift -0.8% > -1.0% threshold → NO
        → XEF.TO drift +0.3% > -1.0% threshold → NO
        → Sort score: (0, 3800)
  → acct_B ranks first! (has underweight alternatives)
  → SELL 200 VUN.TO from acct_B (all it has) → ledger[acct_B][CAD] += $12,436
  → Remaining: 402 - 200 = 202 shares still needed
  → SELL 202 VUN.TO from acct_A → ledger[acct_A][CAD] += $12,560

Round 1 — Layer 2 (Buys):
  QQQ is underweight (-2.0% < -1.0% threshold)
  → shares_for_drift_gap($1M, -2.0%, US$512, "USD", 1.36) = 28 shares
  → _eligible_buy_accounts: only acct_B holds QQQ
  → _buy_in_account(acct_B):
      total_buying_power = $0 USD + cross-currency from $12,636 CAD ≈ US$9,291
      affordable = floor(US$9,291 / US$512) = 18 shares
      quantity = min(28, 18) = 18  ← cash-limited
      fund_buy: $0 native USD < cost → converts all from CAD
  → BUY 18 QQQ in acct_B (FX)
  → Remaining 10 QQQ unfilled (not enough cash anywhere that holds QQQ)

  ZAG.TO drift is -0.8% → NOT < -1.0% threshold → skipped by Layer 2
  (Layer 2 only acts on symbols beyond the drift_trade_threshold_pct)

Round 1 — Layer 3 (Residual Cash):
  _deploy_residual_cash runs after the starter passes
  
  acct_B: All CAD was consumed by the QQQ FX conversion. ~$0 left. Done.
  
  acct_A: Has $12,560 + $300 = $12,860 CAD from VUN.TO sale proceeds!
  → build_deploy_cash_underweight(acct_A, CAD, threshold=0.0):
      → underweight_candidates: ZAG.TO drift -0.8% < -0.0% → underweight!
      → shares_to_close_underweight: ceil(0.8% × $1M / $10.85) = 738 shares
      → affordable = floor($12,860 / $10.85) = 1,185 shares
      → quantity = min(738, 1,185) = 738  ← capped at drift gap
  → BUY 738 ZAG.TO in acct_A ("Deploy cash (underweight)")

  acct_A: Still has $12,860 - (738 × $10.85) = $4,857 CAD remaining
  → build_deploy_cash_underweight (same-currency): ZAG.TO drift now ≈ 0% → not underweight → skip
  → _build_deploy_cash_any:
      → ZAG.TO (~0%), XEF.TO (+0.3%), VUN.TO (~0%) all buyable
      → ZAG.TO or VUN.TO (both ~0%) → lowest drift wins
      → BUY 78 VUN.TO in acct_A ("Deploy cash (any)")
  → Remaining ~$1,006 < $10.85 (ZAG.TO) → done

Round 2 — Layer 1 (Sells):
  VUN.TO: was +2.5%, sold 402 shares → drift near 0%. NOT > threshold.
  Nothing overweight enough → 0 changes → loop exits.

Trade netting:
  acct_B VUN.TO: SELL 200 (single trade, pass-through)
  acct_B QQQ: BUY 18 (single trade, pass-through)
  acct_A VUN.TO: SOLD 202 + BOUGHT 78 = NET SELL 124 VUN.TO
  acct_A ZAG.TO: BUY 738 (single trade, pass-through)

Final trades presented to user:
  SELL 200 VUN.TO in Bob Margin
  SELL 124 VUN.TO in Alice RRSP
  BUY 18 QQQ in Bob Margin (requires currency conversion)
  BUY 738 ZAG.TO in Alice RRSP
```

**Key insights:**

1. **`allocate_sell` drove the cross-account split.** Account B got priority (it had QQQ as an underweight alternative), exhausted its 200 shares, then the remainder spilled to Account A. Without the "prefer accounts with underweight alternatives" heuristic, all 402 shares would have sold from A (larger position) and B's QQQ would have been unfunded.

2. **Each account's proceeds funded different symbols.** B's $12,436 went to QQQ (cross-currency). A's $12,560 went to ZAG.TO (same-currency). One overweight position → two different underweight corrections in two accounts.

3. **ZAG.TO was below the starter threshold but above the residual threshold.** At -0.8%, ZAG.TO didn't qualify for a Layer 2 "underweight buy" (which requires drift < -1.0%). But Layer 3's residual cash deployment uses threshold 0.0% — any negative drift qualifies. This is Rule 4 in action: "minimize free cash whenever practical."

4. **Trade netting cleaned up Account A.** The planner sold 202 VUN.TO, then bought back 78 as "best available." Netting collapses this to a clean SELL 124. The user sees the net effect, not the intermediate churn.

---

### Scenario 3: Multi-Account Cascade — One Sell Triggers a Chain Reaction

**Setup:** A portfolio with three accounts where a single overweight position triggers trades across all accounts through the funding-sell mechanism.

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
  → allocate_sell: acct_B has 2,000 VUN.TO + underweight ZAG.TO (-2.1%)
                   acct_A has 500 VUN.TO + no underweight alternatives
  → acct_B ranks first (has underweight alternatives where proceeds can go)
  → SELL 96 VUN.TO from acct_B → $5,969 CAD in acct_B

Round 1 — Layer 2 (Buys):
  ZAG.TO is -2.1% underweight (most underweight → first in queue)
  → shares_for_drift_gap($750K, -2.1%, $10.85, CAD) = 1,451 shares
  → _eligible_buy_accounts: acct_C has ZAG.TO + ~$24,004 CAD (from XEF.TO sell)
                             acct_B has ZAG.TO + ~$6,049 CAD (from VUN.TO sell)
  → acct_C has more same-currency cash → fills first
  → _buy_in_account(acct_C): affordable = floor($24,004 / $10.85) = 2,212
      → quantity = min(1,451, 2,212) = 1,451 — capped at drift gap
      → BUY 1,451 ZAG.TO in acct_C (same-currency, no FX)
      → Ledger: acct_C CAD reduced to ~$8,261
  → remaining = 0 → ZAG.TO fully filled from acct_C alone

  QQQ is -1.9% underweight
  → shares_for_drift_gap($750K, -1.9%, US$512, USD, 1.36) = 20 shares
  → _eligible_buy_accounts: only acct_C holds QQQ
  → _buy_in_account(acct_C):
      total_buying_power = US$200 native + convertible from ~$8,261 CAD ≈ US$6,200
      affordable = floor($6,200 / $512) = 12 shares
      quantity = min(20, 12) = 12  ← limited by available cash!
      _raise_cash_in_account: looks for same-currency (USD) overweight
        positions in acct_C — none exist (only CAD positions). No help.
      → BUY 12 QQQ in acct_C (FX)
      → Remaining 8 shares unfilled (no more accounts hold QQQ)

Round 1 — Layer 3 (Residual Cash):
  acct_B still has ~$6,049 CAD (VUN.TO sell proceeds not yet spent)
  → build_deploy_cash_underweight (same-currency): ZAG.TO at ~0% is NOT underweight → skip
  → _build_deploy_cash_any:
      ZAG.TO (~0%) is "best available" in acct_B (lowest drift)
      → BUY ~557 ZAG.TO in acct_B ("Deploy cash (any)")
  → This pushes ZAG.TO to ~+0.8% overweight at the household level!

  acct_C has tiny leftover → mop-up trades

Round 2 — Layer 1 (Sells):
  ZAG.TO is now +0.8% overweight (> threshold)
  → shares_for_drift_gap($750K, 0.8%, $10.85, CAD) = ~553 shares to sell
  → allocate_sell: acct_C has 7,451 ZAG.TO + underweight QQQ (-0.7%)!
                   acct_B has 5,557 ZAG.TO + no underweight alternatives
  → acct_C ranks first (has QQQ as underweight alternative)
  → SELL ~553 ZAG.TO from acct_C → ~$6,000 CAD in acct_C

Round 2 — Layer 2 (Buys):
  QQQ is still underweight (~-0.7%)
  → _eligible_buy_accounts: acct_C holds QQQ + now has ~$6,000 CAD
  → total_buying_power ≈ US$4,400 → affordable = 8 more QQQ
  → BUY 8 QQQ in acct_C (FX)
  → QQQ drift now ~0%

Round 2 onwards: all drifts < threshold → converged.

The cascade:
  1. XEF.TO sell from acct_C (has QQQ as underweight alternative) → $24K CAD
  2. VUN.TO sell from acct_B (has ZAG.TO as underweight alternative) → $6K CAD
  3. ZAG.TO buy in acct_C consumes most of the XEF.TO proceeds (Layer 2)
  4. Remaining CAD in acct_C → 12 QQQ (cross-currency, Layer 2)
  5. acct_B's $6K → ZAG.TO "best available" buy (Layer 3) → overshoots ZAG.TO
  6. Round 2: ZAG.TO overweight → sell from acct_C (has QQQ alternative)
  7. Those proceeds fund 8 more QQQ in acct_C → QQQ gap fully closed!
  8. The $6K effectively "routed through" ZAG.TO in acct_B → acct_C → QQQ
```

**Key insight:** No cash moves between accounts — each account acts independently. But *household-level drift measurement* acts as the coordination signal that makes independent per-account decisions produce a globally coherent result. acct_B buys ZAG.TO because that's its only option (Rule 4). This changes the household drift for ZAG.TO, which Round 2 picks up. acct_C then sells its own ZAG.TO because the household says it's overweight and acct_C has a better use for the proceeds (underweight QQQ). Neither account "knows about" the other — they're both just reacting to the same shared drift numbers. The emergent result is that acct_B ends up holding more ZAG.TO and acct_C ends up holding more QQQ, and the QQQ gap gets fully closed across two rounds. This is why the iterative multi-round design matters: Round 1's "best available" overshoot creates a signal that Round 2 can act on.

---

### Scenario 4: The "Best Available" Fallback — Cascade-Aware Candidate Selection

**Setup:** An account holds only two CAD symbols. Both are at or above target. But the account received $2,000 CAD from a dividend or prior sell. There's nothing underweight to buy — but leaving cash idle violates Rule 4 ("Minimize free cash whenever practical").

```
Account "Bob RRSP":
  VUN.TO: 800 shares @ $62.18, drift +0.3%
  XBB.TO: 2,000 shares @ $28.94, drift +0.1%
  Cash CAD: $2,000

Other accounts:
  Account "Alice TFSA": holds VUN.TO (drift +0.3%) and ZAG.TO (drift -0.8%)
  Account "Carol RRSP": holds XBB.TO only (drift +0.1%)

Neither VUN.TO nor XBB.TO is underweight in Bob RRSP. Normal underweight buys return nothing.

Fallback chain:
  1. build_deploy_cash_underweight (same-currency) → no underweight candidates → returns None
  2. _build_deploy_cash_any kicks in (compares same- vs cross-currency by cascade)
     → _account_buyable_candidates(underweight_only=False)
     → Computes _cascade_score for each candidate:
         VUN.TO: Alice TFSA holds VUN.TO and has ZAG.TO at -0.8%
                 → cascade_score = 0.8
         XBB.TO: Carol RRSP holds XBB.TO but has no underweight positions
                 → cascade_score = 0.0
     → Sort key: (-cascade_score, drift_pct)
         VUN.TO: (-0.8, +0.3%) ranks FIRST
         XBB.TO: (-0.0, +0.1%) ranks second
     → BUY 32 VUN.TO @ $62.18 = $1,989.76 ("Deploy cash (any)")

Result: Cash is deployed into VUN.TO, overshooting its household allocation.
Round 2: VUN.TO drift pushes past threshold → sell from Alice TFSA
         (which has ZAG.TO as an underweight alternative)
         → ZAG.TO gap gets filled using those proceeds.
```

**Key insight:** The "best available" fallback doesn't just deploy cash into the *least bad* option — it picks the option most likely to trigger a useful cascade in a subsequent round. The `_cascade_score` of a symbol is the total underweight drift in other accounts that also hold it. Buying a high-cascade symbol now may overshoot the household allocation, which Round 2 corrects by selling from the *right* account — one that has underweight alternatives to absorb the proceeds.

When no symbol has cascade potential (all other holders are balanced), the fallback degrades gracefully to lowest-drift ordering.

---

### Scenario 5: Trade Netting Saves You From Silly-Looking Recommendations

**Setup:** The planner decides to sell VUN.TO from an account in Layer 1 (overweight sell), then in Layer 3 buys back some VUN.TO in the same account with leftover cash. Without netting, the user would see "SELL 200" and "BUY 12" for the same symbol in the same account.

```
Raw trades generated by planner:
  Round 1, Layer 1: SELL 200 VUN.TO in acct_A @ $62.18 ("Overweight sell")
  Round 1, Layer 3: BUY 12 VUN.TO in acct_A @ $62.18 ("Deploy cash (underweight)")

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
  Home currencies = {USD} (only USD positions have a positive target)
  → exactly one home → eligible for sweep; CAD is the stranded side
  CAD cash ($500) > fee ($10.49) → sweep it into USD
  
  Sweep logic:
    cad_for_shares = $500 - $10.49 = $489.51
    sweep_shares = floor($489.51 / $13.79) = 35 DLR.TO shares
    → Convert: Buy 35 DLR.TO @ $13.79 = $482.65 CAD
    → Sell 35 DLR.U.TO @ $10.15 = US$355.25
  
  Conversion recommendation added to the Norbert's Gambit table.
  If the account already needed a CAD→USD conversion for a trade,
  the sweep shares are ADDED to the existing conversion (same journal entry).
```

**Key insight:** The sweep logic in `fx_conversions.py` runs *after* the rebalancer. It detects "orphaned" foreign cash that can never be productively used and folds it into the conversion plan, preventing the slow accumulation of small unusable cash balances over time. Because the rebalancer deploys residual cash only same-currency, the sweep is the sole rescue path for stranded foreign cash — so it must catch every case.

The sweep is gated on the account having **exactly one home currency**, where a "home currency" is one in which the account holds a post-trade position with a positive target (`_positive_target_currency_homes`). The stranded (non-home) currency's cash is swept into the home currency. Three cases follow from this:

- **One home** (e.g. a USD-only account, or a mixed account whose CAD side is all at target while a target-0 USD wind-down is being sold off): the non-home cash is swept. This is the case shown above.
- **Two homes** (a genuinely mixed account with positive targets on *both* sides): neither currency is stranded — the planner deploys each side same-currency — so nothing is swept and no wasteful FX round-trip occurs.
- **Zero homes** (only cash and/or target-0 wind-down positions): neither currency has a deployable home, so converting would just strand the cash in the other currency. Nothing is swept.

Two subtleties make this robust:

1. **Post-trade positions.** Home detection uses quantities *after* the planned trades. A target-0 position (e.g. an untargeted AMZN) fully sold within the plan correctly stops anchoring its currency as a home, so its USD proceeds sweep into the real (CAD) home rather than strand. This also relies on `shares_for_drift_gap` fully liquidating such positions — see [the floating-point nudge](#fp-nudge) in the sizing math — otherwise a 1-share sliver would keep the currency looking occupied.
2. **Target-0 positions never count as homes.** They are only ever sold, never bought, so cash in their currency has nowhere to be deployed.

The sweep is symmetric: the example above shows a USD-home account sweeping leftover CAD, but a CAD-home account with leftover USD sweeps the other direction (Buy DLR.U.TO → Sell DLR.TO) the same way.

---

### How These Scenarios Interact With the Rules

| Scenario | Primary Rules Exercised |
|----------|------------------------|
| 1. Large cash deposit (single USD stock) | Rule 5 ("Only buy symbols that already exist in the account"), Rule 7 ("Allow account-constrained cash deployment, then clean up globally"), Rule 4 ("Minimize free cash whenever practical") |
| 2. Sell allocation split | Rule 2 ("Sell overweight positions and buy underweight positions"), Rule 5 ("Only buy symbols that already exist in the account"), Rule 4 ("Minimize free cash whenever practical"), Rule 8 ("Avoid obviously wasteful churn") |
| 3. Multi-account chain reaction | Rule 1 ("Treat all accounts as one household portfolio"), Rule 5 ("Only buy symbols that already exist in the account"), Rule 6 ("Prefer same-currency deployment before cross-currency deployment") |
| 4. Best-available cash deployment | Rule 4 ("Minimize free cash whenever practical"), Rule 5 ("Only buy symbols that already exist in the account") |
| 5. Trade netting | Rule 8 ("Avoid obviously wasteful churn") |
| 6. Cross-currency sweep | Rule 4 ("Minimize free cash whenever practical"), Rule 6 ("Prefer same-currency deployment before cross-currency deployment") |

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
│   ├── _raise_cash_in_account()    # Funding sells for cash
│   ├── _deploy_residual_cash()     # Layer 3: spend remaining cash
│   ├── _cascade_score()            # Score a symbol by downstream rebalancing potential
│   ├── _account_buyable_candidates()  # Find buyable symbols in an account
│   └── _build_deploy_cash_any()   # Deploy leftover cash to highest-cascade holding (same- or cross-currency)
├── shares_for_drift_gap()          # Sizing: shares needed for drift
├── max_sellable_without_crossing_target() # Sizing: max safe sell
├── allocate_sell()                 # Multi-account sell distribution
├── find_accounts_for_symbol()      # Account lookup
├── get_position_quantity()         # Position lookup
├── effective_qty()                 # Position adjusted for planned trades
└── _has_underweight_alternatives() # Can proceeds be redeployed?
```
