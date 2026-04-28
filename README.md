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
- **Configurable Drift Trade Threshold** — Only recommends trades when a position's absolute drift meets your configured minimum threshold
- **Tolerance-Aware Status Display** — The allocation tables mark symbols as `OK`, `OVER`, or `UNDER` using your configured drift tolerance
- **Conservative FX Funding** — Currency conversion suggestions use conservative DLR bid/ask math so Norbert's Gambit sizing does not underfund buys
- **Sell Trimming Reconciliation** — After baseline trade generation, oversized sells are trimmed when possible so they do not raise materially more cash than the planned buys require
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

Edit the static target percentages, then optionally add FX-driven target rules.
The static targets plus any enabled FX-derived targets should sum to 100%
after resolution:

```yaml
targets:
  CAD: 0.0      # Cash CAD
  USD: 0.0      # Cash USD
  VCN.TO: 2.0
  VUN.TO: 2.0
  XEF.TO: 2.0
  XEC.TO: 6.0
  XBB.TO: 6.0
  CASH.TO: 8.0

fx_target_rules:
  ivv_vsp:
    enabled: true
    usd_symbol: IVV
    cad_symbol: VSP.TO
    total_target_pct: 74.0
    min_usd_to_cad_rate: 1.0
    max_usd_to_cad_rate: 1.5
    target_rounding_decimals: 0

# List symbols to temporarily exclude from rebalancing.
# e.g., DLR.TO or DLR.U.TO mid-Norbert's Gambit.
transient_symbols:
  - DLR.TO
  - DLR.U.TO

# Trading fee used for Norbert's Gambit conversion suggestions
norberts_gambit_fee_cad: 10.49

# Only trade symbols whose absolute drift is at least this %
drift_trade_threshold_pct: 0.5
```

**Transient symbols:** List any symbol in `transient_symbols` that you're holding temporarily (e.g., DLR.TO / DLR.U.TO mid-Norbert's Gambit). Transient symbols are excluded from trading but their value stays in the portfolio total so allocation math remains correct. Remove them once you've sold manually.

**FX target rules:** `fx_target_rules` lets you derive part of the portfolio target automatically from the live USD/CAD exchange rate. In the example above, IVV and VSP.TO share a combined 74% target, with more allocated to VSP.TO as USD becomes more expensive relative to CAD.

**Unknown holdings:** Any symbol you hold that isn't in `targets` (and isn't transient) gets an implicit 0% target — the rebalancer will recommend selling it.

**Norbert's Gambit fee:** `norberts_gambit_fee_cad` controls the estimated trading cost used when reporting currency conversion needs.

**Drift trade threshold:** `drift_trade_threshold_pct` controls how far a symbol must drift from target before the rebalancer will act on it. The allocation tables also use this same threshold when showing `OK`, `OVER`, and `UNDER`. For example, `0.5` means a symbol at `+0.4%` or `-0.4%` drift is treated as within tolerance.

## Project Structure

```
investment-rebalancer/
├── .gitignore                      # Ignore local / generated files
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
│   ├── rebalancer_reconcile.py    # Post-processing trim of excess sell funding
│   ├── rebalancer_simulation.py   # Projected post-trade allocations
│   ├── rules.py                   # Trade placement rules engine
│   ├── currency.py                # USD/CAD exchange rate & Norbert's Gambit
│   ├── funding.py                 # Shared fee-aware funding / conversion helpers
│   ├── report_builder.py          # Assembles report data for the CLI
│   ├── history.py                 # Portfolio history / all-time high tracking
│   └── display.py                 # Rich terminal output
├── .github/workflows/
│   ├── portfolio_sync.yml         # Twice-daily portfolio sync (token refresh + history snapshot)
│   └── cleanup-runs.yml           # Monthly cleanup of old GitHub Actions runs
├── main.py                        # Entry point
└── requirements.txt
```

## GitHub Actions

- **`portfolio_sync.yml`** runs twice daily (3:00 AM and 3:00 PM PDT) to keep Questrade OAuth tokens fresh and snapshot the portfolio value for ATH tracking. The Questrade API goes offline periodically, so an alert issue is only created if syncs fail for more than 48 consecutive hours.
- **`cleanup-runs.yml`** runs monthly to delete older GitHub Actions workflow runs, keeping the most recent history while reducing Actions clutter.

When running locally, `python main.py` automatically pulls the latest tokens from the remote before connecting to Questrade, and pushes the refreshed tokens and updated portfolio history back when done.

## ⚠️ Security

This repo stores Questrade refresh tokens in `tokens/`. **Keep this repository PRIVATE.** The tokens are committed to the repo so GitHub Actions can refresh them automatically.

## Accuracy Score

The accuracy score is calculated as:

```
Accuracy = 100% - (sum of absolute drifts / 2)
```

- **100%** = perfectly balanced
- **98%+** = very close to target
- **95-98%** = minor drift, probably fine
- **<95%** = rebalance recommended