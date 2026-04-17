# Investment Rebalancer

A Python-based portfolio rebalancer for Questrade accounts. Replaces Passiv with a streamlined, customized tool that treats multiple accounts across two Questrade logins as one unified portfolio.

## Features

- **Unified Portfolio View** — Aggregates all accounts (RRSP, TFSA, FHSA, Margin, RESP, LIRA, Corporate) across two Questrade logins into a single portfolio
- **Target Allocation Tracking** — Compares current holdings against configurable target percentages
- **Accuracy Score** — Single percentage showing how close the portfolio is to the target (100% = perfectly balanced)
- **Smart Trade Placement** — Only recommends trades in accounts that already hold the position (never introduces new tickers into an account)
- **Currency Handling** — Detects USD/CAD conversion needs and flags Norbert's Gambit status
- **Transient Symbols** — Temporarily exclude symbols from rebalancing (e.g., DLR.TO / DLR.U.TO mid-Norbert's Gambit). Value stays in the portfolio total so allocation math is correct
- **Unknown Holdings** — Any symbol not in targets (and not transient) is treated as 0% target and sold automatically
- **Projected Accuracy** — Shows what the accuracy would be after executing recommended trades
- **Whole-Share Trading** — Recommends whole shares only, using bid price for sells and ask price for buys
- **Iterative Algorithm** — Repeats Sell → Buy → Sweep rounds until all positions are within tolerance, handling same-currency, cross-currency, and displacement trades in a single unified pass
- **±0.1% Drift Tolerance** — Positions within tolerance are left alone to avoid unnecessary trades
- **Automatic Portfolio Sync** — GitHub Actions cron job refreshes Questrade OAuth tokens and snapshots portfolio value twice daily

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Set Up Questrade API Tokens

1. Go to [Questrade API Hub](https://login.questrade.com/APIAccess/UserApps.aspx) for each account
2. Create a personal app (or use an existing one)
3. Generate a refresh token
4. Paste the refresh token into the corresponding file:

```bash
# tokens/jeff_token.json
{"refresh_token": "YOUR_TOKEN_HERE"}

# tokens/eunee_token.json
{"refresh_token": "YOUR_TOKEN_HERE"}
```

### 3. Run the Rebalancer

```bash
python main.py
```

### 4. Scheduled Sync Mode (used by GitHub Actions)

```bash
python main.py --sync
```

## Configuration (`config/targets.yaml`)

Edit target percentages (must sum to 100%):

```yaml
targets:
  CAD: 0.0      # Cash CAD
  USD: 0.0      # Cash USD
  VCN.TO: 2.0
  VUN.TO: 2.0
  IVV: 19.0
  XEF.TO: 2.0
  VSP.TO: 55.0
  XEC.TO: 6.0
  XBB.TO: 6.0
  CASH.TO: 8.0

# List symbols to temporarily exclude from rebalancing.
# e.g., DLR.TO or DLR.U.TO mid-Norbert's Gambit.
transient_symbols:
  - DLR.TO
  - DLR.U.TO

# Trading fee used for Norbert's Gambit conversion suggestions
norberts_gambit_fee_cad: 10.49
```

**Transient symbols:** List any symbol in `transient_symbols` that you're holding temporarily (e.g., DLR.TO / DLR.U.TO mid-Norbert's Gambit). Transient symbols are excluded from trading but their value stays in the portfolio total so allocation math remains correct. Remove them once you've sold manually.

**Unknown holdings:** Any symbol you hold that isn't in `targets` (and isn't transient) gets an implicit 0% target — the rebalancer will recommend selling it.

**Norbert's Gambit fee:** `norberts_gambit_fee_cad` controls the estimated trading cost used when reporting currency conversion needs.

## Project Structure

```
investment-rebalancer/
├── config/
│   └── targets.yaml              # Target allocations
├── data/
│   └── portfolio_history.json    # Portfolio value history for ATH tracking
├── tokens/
│   ├── jeff_token.json            # Jeff's Questrade refresh token
│   └── eunee_token.json           # Eunee's Questrade refresh token
├── src/
│   ├── questrade_client.py        # Questrade API wrapper
│   ├── portfolio.py               # Portfolio aggregation & accuracy
│   ├── rebalancer.py              # Public rebalancer API
│   ├── rebalancer_core.py         # Shared rebalance state & helpers
│   ├── rebalancer_steps.py        # Sell / buy / sweep phases
│   ├── rebalancer_netting.py      # Final trade netting
│   ├── rebalancer_simulation.py   # Projected post-trade allocations
│   ├── rules.py                   # Trade placement rules engine
│   ├── currency.py                # USD/CAD exchange rate & Norbert's Gambit
│   ├── report_builder.py          # Assembles report data for the CLI
│   ├── report_models.py           # Report data models
│   ├── history.py                 # Portfolio history / all-time high tracking
│   └── display.py                 # Rich terminal output
├── .github/workflows/
│   └── portfolio_sync.yml         # Twice-daily portfolio sync (token refresh + history snapshot)
├── main.py                        # Entry point
└── requirements.txt
```

## GitHub Actions Portfolio Sync

The workflow in `.github/workflows/portfolio_sync.yml` runs twice daily (3:00 AM and 3:00 PM PDT) to keep Questrade OAuth tokens fresh and snapshot the portfolio value for ATH tracking. The Questrade API goes offline periodically, so an alert issue is only created if syncs fail for more than 48 consecutive hours.

When running locally, `python main.py` automatically pulls the latest tokens from the remote before connecting to Questrade, and pushes the refreshed tokens and updated portfolio history back when done.

## ⚠️ Security

This repo stores Questrade refresh tokens in `tokens/`. **Keep this repository PRIVATE.** The tokens are committed to the repo so GitHub Actions can refresh them automatically.

## Accuracy Score

The accuracy score is calculated as:

```
Accuracy = 100% - (sum of absolute drifts / 2)
```

- **100%** = perfectly balanced
- **95%+** = minor drift, probably fine
- **90-95%** = moderate drift, consider rebalancing
- **<90%** = significant drift, rebalance recommended