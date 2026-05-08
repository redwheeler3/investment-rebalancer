# Architecture & Data Flow

A deep dive into how the investment rebalancer works — following the code from startup to final output, module by module.

---

## High-Level Pipeline

```
┌─────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Config &   │───▶│  Connect &   │───▶│   Build      │───▶│  Calculate   │───▶│   Render &   │
│  Startup    │    │  Auth        │    │  Portfolio   │    │  Trades      │    │   Persist    │
└─────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

Each stage produces data that feeds into the next. There are no circular dependencies — data flows strictly left to right.

---

## It Starts in `main.py`

The `run_rebalancer()` function is the spine of the app. Every other module is called from here in sequence:

```python
def run_rebalancer():
    accounts, targets, transient_symbols, ... = load_config()

    clients = _connect_clients(accounts)
    usd_to_cad_rate = _fetch_exchange_rate(clients[0])
    resolved_targets = resolve_targets(targets, fx_target_rules, usd_to_cad_rate)
    portfolio = _build_priced_portfolio(clients, usd_to_cad_rate)
    dlr_quotes = _fetch_dlr_quotes(clients[0])

    trades = calculate_trades(portfolio, resolved_targets, ...)
    currency_conversions = calculate_currency_needs(trades, portfolio.accounts, ...)
    report = build_report_data(portfolio, trades, ...)
    record_value(portfolio.total_value_cad)
    _render_report(portfolio, report, ...)
```

Before any of this, `_pull_latest()` does a `git pull --ff-only` on the private state repo to get fresh tokens and history. After everything, `_push_synced_files()` commits and pushes the rotated tokens + updated history back.

---

## Stage 1: Configuration (`main.py` → `paths.py` → `fx_targets.py`)

### Where is everything?

`paths.py` resolves the private state repo location from the `REBALANCER_STATE_DIR` environment variable:

```python
def get_state_root() -> Path:
    raw = os.environ.get(ENV_VAR_NAME)
    if not raw:
        raise RuntimeError(f"{ENV_VAR_NAME} is not set...")
    return Path(raw).expanduser().resolve()
```

All mutable files (tokens, config, history) live in that separate repo — the code repo stays clean and public.

### Loading config

`load_config()` reads `config/settings.yaml` and extracts:
- **accounts** — who you are, where your tokens live
- **targets** — desired allocation percentages (e.g., `VSP.TO: 53.0`)
- **fx_target_rules** — dynamic allocation splits based on exchange rate
- **transient_symbols** — things like DLR.TO you're temporarily holding during a conversion
- **drift_trade_threshold_pct** — minimum drift before a trade is recommended
- **norberts_gambit_fee_cad** — per-conversion fee
- **tactical_config** — drawdown-based regime state machine that dynamically shifts the fixed/equity split (parsed via `parse_tactical_config()`)

Optional fields gracefully default:
```python
transient_symbols = data.get("transient_symbols", [])
norberts_gambit_fee_cad = data.get("norberts_gambit_fee_cad", 0.0)
fx_target_rules = data.get("fx_target_rules", {})
tactical_config = parse_tactical_config(data.get("tactical_deployment", {}))
```

### Tactical deployment

The `tactical_deployment` section is parsed by `parse_tactical_config()` in `tactical.py`. When `enabled: true`, it returns a `TacticalConfig` dataclass; otherwise `None` (feature is off). This implements a drawdown-based regime state machine that dynamically shifts the portfolio's fixed-income / equity split — deploying bonds into equities during crashes, and rebuilding the bond position on recovery.

### Deep Dive: Tactical Deployment

The tactical deployment system is a regime-based state machine that dynamically shifts the portfolio's fixed-income / equity split in response to drawdowns. Instead of a simple "sell bonds, buy stocks" rule, it implements a multi-level deployment with hysteresis to avoid whipsawing.

#### The Problem It Solves

During a market crash, you want to deploy your fixed-income holdings into equities while they're cheap. But you don't want to:
- Deploy too early (what if the crash deepens?)
- Deploy too late (you miss the recovery)
- Whipsaw back and forth near thresholds (expensive and stressful)

The solution: a **tiered deployment** with **separate deploy/recovery thresholds** (hysteresis), triggered by drawdown from a frozen Reference High.

#### Regime State Machine

Four possible regimes, each with a different fixed-income percentage:

```
baseline (20% fixed) → level_1 (15%) → level_2 (10%) → level_3 (5%)
```

Transitions happen **one level at a time** per evaluation. Even if the portfolio drops 30% in one day, it moves `baseline → level_1` on the first evaluation, then `level_1 → level_2` on the next, etc. This prevents a flash crash from triggering full deployment in a single run.

#### Reference High & Drawdown

The **Reference High** determines the baseline from which drawdown is measured:

- **At baseline:** Reference High = the all-time high (ATH). It tracks new highs as the portfolio grows.
- **When deployed:** Reference High **freezes** at the ATH value when deployment first triggered. This prevents the reference from rising while deployed, which would trap you in a deployed state.

```python
# At baseline, reference tracks ATH
if state.regime == "baseline":
    reference_high = ath_value       # Moves up with new highs

# When deployed, reference is frozen
else:
    reference_high = state.reference_high  # Fixed at deployment moment
```

Drawdown is then:
```python
drawdown_pct = ((current_value - reference_high) / reference_high) * 100.0
```

#### Hysteresis (Why Two Sets of Thresholds)

Deploy thresholds trigger going **down** (into drawdown). Recovery thresholds trigger going **up** (out of drawdown). They're deliberately offset:

```
Deploy:    -10% → level_1,   -20% → level_2,   -30% → level_3
Recovery:  -15% → level_1,    -5% → level_1,    +5% → baseline
```

This means:
- You deploy to level_1 at -10% drawdown
- You don't recover back to baseline until +5% **above** the reference high
- Between -10% and +5%, you stay deployed — no thrashing

The gap between deploy and recovery thresholds is the **dead zone** where no transitions happen.

#### Example Walkthrough (With Numbers)

Portfolio ATH: $1,000,000 on Jan 15, 2026. This scenario shows a crash, partial recovery that doesn't reach baseline, a second leg down, and then full recovery — demonstrating how the reference stays frozen and hysteresis prevents whipsawing.

| Date | Value | Drawdown | Regime | Fixed % | What Happened |
|------|-------|----------|--------|---------|---------------|
| Jan 15 | $1,000,000 | 0% | baseline | 20% | ATH day |
| Mar 1 | $900,000 | -10% | level_1 | 15% | Deploy trigger! Reference freezes at $1M |
| Mar 15 | $800,000 | -20% | level_2 | 10% | Deeper deploy |
| Apr 1 | $700,000 | -30% | level_3 | 5% | Max deployment |
| Apr 15 | $750,000 | -25% | level_3 | 5% | Still deep, no recovery |
| May 1 | $850,000 | -15% | level_2 | 10% | Recovery trigger (-15% threshold) |
| May 15 | $950,000 | -5% | level_1 | 15% | Recovery trigger (-5% threshold) |
| Jun 1 | $980,000 | -2% | level_1 | 15% | Rising but not at +5% → stays level_1 |
| Jun 15 | $920,000 | -8% | level_1 | 15% | Dips again but NOT past -10% → no redeploy |
| Jul 1 | $800,000 | -20% | level_2 | 10% | Second leg down, deploy trigger again |
| Jul 15 | $700,000 | -30% | level_3 | 5% | Max deployment again |
| Aug 15 | $850,000 | -15% | level_2 | 10% | Recovery begins |
| Sep 15 | $950,000 | -5% | level_1 | 15% | Continuing recovery |
| Oct 15 | $1,050,000 | +5% | baseline | 20% | Full recovery! Reference unfreezes |

Key observations:
- Reference stayed frozen at $1M for the **entire period** (Mar 1 → Oct 15) — it never updated to a new ATH during deployment
- On Jun 15 the portfolio dipped to -8%, but since level_1's deploy threshold is -10%, it stayed at level_1 — hysteresis prevented re-deployment
- On Jun 1 the portfolio was only -2% from reference, but the recovery threshold to baseline is +5%, so it stayed at level_1 — hysteresis prevented premature recovery
- The second leg down (Jul 1) re-triggered deployment because the drawdown exceeded thresholds again
- The system never jumps levels — each evaluation can move at most one step

#### State Persistence

The regime state is persisted in `data/tactical_state.json`:

```json
{
  "regime": "level_1",
  "reference_high": 1000000.0,
  "reference_high_date": "2026-01-15",
  "last_transition_date": "2026-03-01"
}
```

At baseline, the file is minimal:
```json
{
  "regime": "baseline"
}
```

The reference high and date are only stored when deployed (they're derived from ATH at baseline).

#### Target Resolution (`resolve_tactical_targets()`)

Once the posture is known, targets are adjusted:

1. **Fixed-income targets** are set absolutely from `fixed_composition × fixed_pct`:
   ```python
   # At level_1 (15% fixed):
   ZMMK.TO → 15% × 0.50 = 7.5%
   XSH.TO  → 15% × 0.25 = 3.75%
   XIGS.TO → 15% × 0.25 = 3.75%
   ```

2. **Equity targets** are scaled proportionally to fill the remaining space:
   ```python
   # Original equity sum = 80%, new equity target = 85%
   scale_factor = 85.0 / 80.0 = 1.0625
   VSP.TO: 53.0% × 1.0625 = 56.31%
   IVV:    21.0% × 1.0625 = 22.31%
   XEF.TO:  6.0% × 1.0625 = 6.38%
   ```

3. **Cash targets** (CAD, USD) pass through unchanged.

The result always sums to 100%.

#### The `--sync` Mode Connection

Daily `--sync` runs evaluate tactical transitions even when you don't run the full rebalancer. This ensures drawdown triggers are caught promptly:

```python
# In sync mode:
if tactical_config:
    posture = evaluate_tactical_posture(
        current_value, ath_value, ath_date, config=tactical_config
    )
    if posture.transition_occurred:
        print(f"  ⚡ Tactical regime changed: {posture.previous_regime} → {posture.regime}")
```

The next full rebalancer run will see the updated regime and calculate trades accordingly.

#### Display Integration

The terminal report shows:
- Current regime and fixed/equity split
- Drawdown from Reference High
- Next deploy trigger (dollar value where the next level activates)
- Recovery triggers (what needs to happen to step back)

This gives you full visibility into where the system stands without needing to manually check thresholds.

### FX target resolution

This is one of the coolest parts. Instead of hardcoding "21% IVV, 53% VSP.TO", you configure a **total S&P 500 allocation** and let the exchange rate determine the split:

```python
# fx_targets.py
clamped_rate = _clamp(usd_to_cad_rate, min_rate, max_rate)
cad_fraction = (clamped_rate - min_rate) / (max_rate - min_rate)
cad_target_pct = round(total_target_pct * cad_fraction, rounding_decimals)
usd_target_pct = round(total_target_pct - cad_target_pct, rounding_decimals)
```

When USD is expensive (rate near max), more allocation goes to the CAD fund. When USD is cheap, more goes to the USD fund. The targets dynamically adapt to make currency conversion worthwhile.

### Deep Dive: FX Target Resolution

The FX target system is one of the more subtle design decisions. Instead of manually adjusting your allocation split between Canadian and US funds when the exchange rate changes, the app does it automatically.

#### The Problem It Solves

Say you want 74% of your portfolio in "S&P 500 exposure" — split between a Canadian-listed ETF (VSP.TO) and a US-listed one (IVV). The optimal split depends on the exchange rate:

- When USD is expensive (1.45 CAD/USD), you'd prefer to hold more VSP.TO (no conversion needed)
- When USD is cheap (1.20 CAD/USD), you'd prefer more IVV (get US exposure at a discount)
- In between, blend proportionally

#### How It Works (With Numbers)

```yaml
# In settings.yaml:
fx_target_rules:
  sp500_split:
    enabled: true
    cad_symbol: VSP.TO
    usd_symbol: IVV
    total_target_pct: 74.0
    min_usd_to_cad_rate: 1.20
    max_usd_to_cad_rate: 1.50
    target_rounding_decimals: 2
```

At runtime with USD/CAD = 1.36:

```python
clamped_rate = clamp(1.36, 1.20, 1.50) = 1.36
cad_fraction = (1.36 - 1.20) / (1.50 - 1.20) = 0.16 / 0.30 = 0.5333
cad_target_pct = round(74.0 * 0.5333, 2) = 39.47%  → VSP.TO
usd_target_pct = round(74.0 - 39.47, 2) = 34.53%   → IVV
```

At USD/CAD = 1.45 (near max):
```
cad_fraction = (1.45 - 1.20) / (1.50 - 1.20) = 0.8333
VSP.TO → 61.67%, IVV → 12.33%
```

At USD/CAD = 1.22 (near min):
```
cad_fraction = (1.22 - 1.20) / (1.50 - 1.20) = 0.0667
VSP.TO → 4.93%, IVV → 69.07%
```

#### Why Clamping Matters

Without clamping, a rate of 1.10 would produce negative CAD fractions. The `_clamp` ensures the rate stays within the configured range — outside that range, the allocation pins to one extreme.

#### Validation Rules

The resolver enforces several safety rules:
- `usd_symbol` and `cad_symbol` must not also appear in the static `targets` (prevents double-counting)
- `max_rate > min_rate` (prevents division by zero)
- `total_target_pct >= 0` (no negative allocations)
- Both symbols must be defined (no partial rules)

---

## Stage 2: Authentication (`questrade_client.py`)

Questrade uses **single-use refresh tokens** — each time you authenticate, the old token is invalidated and a new one is returned. This means:

1. Every run consumes the token
2. The new token must be saved immediately
3. If a run crashes mid-way, the old token is already dead

```python
def _authenticate(self):
    resp = requests.get(QUESTRADE_AUTH_URL, params={
        "grant_type": "refresh_token",
        "refresh_token": self.refresh_token,
    })
    resp.raise_for_status()
    token_data = resp.json()

    self.access_token = token_data["access_token"]
    self.api_server = token_data["api_server"]      # Dynamic! Changes each auth
    self.refresh_token = token_data["refresh_token"]  # New single-use token
    self._save_refresh_token()  # Write immediately
```

Cool detail: Questrade assigns a **different API server URL** each time you authenticate. You can't hardcode the base URL.

---

## Stage 3: Portfolio Construction (`portfolio.py` → `fx_rate.py`)

### Exchange rate first

Before building the portfolio, we need the USD/CAD rate to value everything in CAD. It's derived from DLR quotes:

```python
# fx_rate.py
cad_bid, cad_ask = _get_dlr_quote(client, "DLR.TO")
usd_bid, usd_ask = _get_dlr_quote(client, "DLR.U.TO")

cad_mid = (cad_bid + cad_ask) / 2   # ~$13.825
usd_mid = (usd_bid + usd_ask) / 2   # ~$10.155
rate = cad_mid / usd_mid             # ~1.3614
```

Using DLR instead of a generic forex feed means the exchange rate is **achievable** — it's the rate you'd actually get doing Norbert's Gambit.

### Building the portfolio

`build_portfolio()` makes API calls for every account across every login, then aggregates:

```python
# For each client (login):
accounts = client.get_accounts()                    # List all accounts under this login
symbols = client.get_symbols(all_symbol_ids)        # Get currency metadata
positions = client.get_positions(account_number)    # Get what's held
balances = client.get_balances(account_number)      # Get cash

# Then for each position:
currency = _resolve_position_currency(pos, symbol_id, symbol, symbol_currency_map)
value_cad = market_value * usd_to_cad_rate if currency == "USD" else market_value
```

The result is a `PortfolioSummary` with two views:
- **Per-account:** Each `AccountInfo` has its own positions and cash (used for trade placement)
- **Aggregated:** `holdings["VSP.TO"]` shows total across all accounts (used for drift calculation)

### Quote enrichment

After the portfolio is built with position-reported prices, we fetch fresh bid/ask quotes:

```python
# portfolio.py
quotes = client.get_quote(symbol_ids)
for quote in quotes:
    holding.bid_price = float(quote.get("bidPrice") or 0)
    holding.ask_price = float(quote.get("askPrice") or 0)
```

This matters because sells use the **bid** (lower) and buys use the **ask** (higher) — gives conservative trade sizing.

### Transient symbol handling

`models.py` checks which configured transient symbols are actually held:

```python
# models.py
def get_transient_status(portfolio, transient_symbols):
    for symbol in transient_symbols:
        if symbol not in portfolio.holdings:
            continue  # Not held — nothing to do
        held.add(symbol)
        # Build alert for display
```

Transient symbols (e.g., DLR.TO mid-conversion) are excluded from rebalancing but still shown in the portfolio value.

---

## Stage 4: Trade Calculation (`rebalancer.py` → `cash_deploy.py` → `fx_math.py`)

See [docs/rebalancer.md](rebalancer.md) for the full algorithm. The key entry point:

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

The rebalancer creates a `RebalancePlanner` which maintains:
- A **TradePlan** — tracks all planned trades and projects the resulting drift
- A **CashLedger** — tracks per-account cash as trades are added

Cool pattern — the `TradePlan` caches the projected allocation snapshot and invalidates it whenever a trade is added:

```python
# rebalancer.py
def add_trade(self, trade):
    self.trades.append(trade)
    self._invalidate()  # Force recalculation of projected drifts

def _invalidate(self):
    self._netted_cache = None
    self._snapshot_cache = None
```

### Currency conversion planning (`fx_conversions.py`)

After trades are finalized, this module figures out the actual DLR share counts needed:

```python
# For each account with a USD shortfall funded by CAD surplus:
shares_needed = ceil(usd_shortfall / usd_sell_price)
shares_affordable = floor((cad_available - fee) / cad_buy_price)
dlr_shares = min(shares_needed, shares_affordable)
```

It also handles **sweep logic** — if an account only holds USD positions but has leftover CAD cash after trades, it converts the remainder to avoid stranded cash.

### The math layer (`fx_math.py`)

Pure functions with no side effects — conversion sizing, buying power calculations:

```python
# How much USD can you get from $60,000 CAD?
def max_usd_from_cad(cad_available, usd_to_cad_rate, fee_cad, dlr_quotes):
    usable_cad = max(0.0, cad_available - fee_cad)
    shares = floor(usable_cad / dlr_quotes.cad_buy_price)
    return shares * dlr_quotes.usd_sell_price
```

All conversions are **conservative** (worst-case pricing) so recommendations are always achievable.

### Deep Dive: The Norbert's Gambit Pipeline

Currency conversion appears in three separate stages, each with a distinct responsibility:

#### Stage 1: During Rebalancing (Conservative Estimation)

The `CashLedger.fund_buy()` and `cross_currency_buying_power()` functions estimate how much target currency you can get from source currency. This uses **conservative** DLR pricing — the ask (buy) price for the source leg and the bid (sell) price for the target leg:

```python
# "How much USD can I get from $60,000 CAD?"
usable_cad = $60,000 - $10.49 fee = $59,989.51
shares = floor($59,989.51 / $13.79)  = 4,350 DLR.TO shares  (pay ask)
usd_received = 4,350 × $10.15        = US$44,152.50          (receive bid)
```

This is intentionally pessimistic so that recommended trades are always achievable at current market prices.

#### Stage 2: Post-Rebalance Conversion Planning (`fx_conversions.py`)

After all trades are finalized, this module calculates the *exact* DLR share counts needed for each account:

```python
# Per-account logic:
net_cash = account.cash - trade_costs + trade_proceeds
if net_cash.usd < 0 and net_cash.cad > 0:
    # Need to convert CAD → USD
    shares_needed = ceil(usd_shortfall / usd_sell_price)    # Minimum shares to cover
    shares_affordable = floor((cad_surplus - fee) / cad_buy_price)  # Max we can buy
    dlr_shares = min(shares_needed, shares_affordable)
```

#### Stage 3: Sweep Logic (Same Module)

After required conversions are planned, the sweep detects leftover cash in the "wrong" currency:

```python
# If all positions are USD but we have leftover CAD:
pos_currencies = {p.currency for p in acct.positions if p.quantity > 0}
if pos_currencies == {"USD"} and remaining_cad > fee:
    # Sweep: convert the rest too
    sweep_shares = floor((remaining_cad - fee) / cad_buy_price)
```

The sweep augments an existing conversion if one was already planned (no extra fee), or creates a standalone conversion if needed.

#### Why Three Stages?

1. **Stage 1** needs to be fast and pessimistic — it's called inside tight loops during planning
2. **Stage 2** runs once, after trades are final — it can be exact
3. **Stage 3** is an optimization — it catches edge cases the planner couldn't handle (because the planner works with projected cash, not actual post-trade cash flows)

---

## Stage 5: Report & Display (`report_builder.py` → `history.py` → `display.py`)

### Report assembly

`build_report_data()` gathers everything the display needs:

```python
# report_builder.py
current_snapshot = build_allocation_snapshot(portfolio, targets, ...)
projected_snapshot = simulate_rebalance(portfolio, trades, targets, ...)
all_time_high = get_all_time_high(current_value=portfolio.total_value_cad)
daily_change = get_daily_change(current_value=portfolio.total_value_cad)
ytd_history = get_year_to_date_history(current_value=portfolio.total_value_cad)
```

All three history functions take the **live portfolio value** directly — they don't depend on what's been written to disk yet. This was a deliberate design choice to avoid ordering bugs.

### History tracking (`history.py`)

Simple JSONL file, one entry per day:
```json
{"date":"2026-01-15","value":1050000.00}
```

ATH detection compares live value against the historical max:
```python
def get_all_time_high(current_value: float) -> AllTimeHigh:
    if current_value >= historical_max:
        return AllTimeHigh(value=current_value, date=today, is_new_ath=True, ...)
    return AllTimeHigh(value=historical_max, date=historical_date, is_new_ath=False, ...)
```

### Display (`display.py`)

Uses the Rich library for terminal rendering. The YTD chart is built entirely in-memory from text characters:

```python
# Build a grid of characters
grid = [[" " for _ in range(chart_width)] for _ in range(chart_height)]

# Plot each data point and fill below it
for x, point in enumerate(series):
    y = value_to_row(point.value)
    grid[y][x] = "░"

# Fill from each point down to the bottom
for y in range(first_drawn_row + 1, chart_height):
    grid[y][x] = "░"
```

The chart auto-sizes to fill the terminal and picks month-start labels that don't overlap.

---

## The `--sync` Mode

GitHub Actions runs `python main.py --sync` on a schedule. This mode:
1. Refreshes all tokens (keeps them alive even when you don't run locally)
2. Snapshots the portfolio value (so ATH tracking works even on days you don't check)
3. Evaluates tactical regime transitions (so drawdown triggers are caught daily)

It's a stripped-down path — no trades calculated, no display rendered. Just auth + record + tactical evaluation.

---

## File Responsibilities

### Source Code (`src/`)

| File | Role |
|------|------|
| `main.py` | Orchestration — wires stages together, handles git sync |
| `questrade_client.py` | API client — auth, positions, quotes |
| `portfolio.py` | Data model + aggregation — builds unified portfolio view |
| `fx_rate.py` | Exchange rate — derives USD/CAD from DLR quotes |
| `fx_targets.py` | Dynamic targets — splits allocations by exchange rate |
| `fx_math.py` | Pure math — conversion sizing, buying power calculations |
| `fx_conversions.py` | Post-trade planning — Norbert's Gambit share counts |
| `rebalancer.py` | Trade decisions — the core algorithm |
| `cash_deploy.py` | Residual cash — spends leftover cash optimally |
| `report_builder.py` | Report assembly — bundles display data |
| `display.py` | Terminal rendering — Rich tables and charts |
| `history.py` | Persistence — ATH tracking, YTD chart data |
| `models.py` | Shared types — TradeRecommendation, TransientAlert |
| `tactical.py` | Tactical deployment — drawdown-based dynamic target adjustment |
| `paths.py` | File locations — private state repo discovery |

---

## Data Flow Diagram

```
settings.yaml
    │
    ▼
load_config() ─────────────────────────────────────────┐
    │                                                    │
    ├─ accounts ──▶ _connect_clients() ──▶ clients      │
    │                                         │          │
    ├─ targets ───┐                           │          │
    │             │                           ▼          │
    ├─ fx_rules ──┼──▶ resolve_targets() ──▶ resolved_targets
    │             │                                      │
    │             │          clients ──▶ get_usd_to_cad_rate()
    │             │                           │          │
    │             │                           ▼          │
    │             │    clients + rate ──▶ build_portfolio()
    │             │                           │          │
    │             │                           ▼          │
    │             │    clients ──▶ fetch_quotes_for_holdings()
    │             │                           │          │
    │             │                           ▼          │
    │             │                      portfolio       │
    │             │                           │          │
    │             └───────────────────────────┼──────────┘
    │                                         │
    ▼                                         ▼
calculate_trades(portfolio, targets, rate, ...) ──▶ trades
    │                                                  │
    ▼                                                  ▼
calculate_currency_needs(trades, accounts, ...) ──▶ conversions
    │                                                  │
    ▼                                                  ▼
build_report_data(portfolio, trades, ...) ──▶ report
    │
    ├──▶ record_value(portfolio.total_value_cad)  [write to disk]
    │
    └──▶ display_full_report(...)  [render to terminal]
```
