# Investment Rebalancer

A Python-based portfolio rebalancer for Questrade accounts. Replaces Passiv with a streamlined, customized tool that treats multiple accounts across two Questrade logins as one unified portfolio.

## Features

- **Unified Portfolio View** — Aggregates all accounts (RRSP, TFSA, FHSA, Margin, RESP, LIRA, Corporate) across two Questrade logins into a single portfolio
- **Target Allocation Tracking** — Compares current holdings against configurable target percentages
- **Accuracy Score** — Single percentage showing how close the portfolio is to the target (100% = perfectly balanced)
- **Smart Trade Placement** — Only recommends trades in accounts that already hold the position (never introduces new tickers into an account)
- **Currency Handling** — Detects USD/CAD conversion needs and flags Norbert's Gambit (DLR.TO/DLR.U.TO) status
- **Projected Accuracy** — Shows what the accuracy would be after executing recommended trades
- **Whole-Share Trading** — Recommends whole shares only, using bid price for sells and ask price for buys
- **Multi-Pass Algorithm** — 7-phase rebalancing: direct sells/buys, cash-raising, displacement recovery, cross-currency, and cash sweep
- **±0.1% Drift Tolerance** — Positions within tolerance are left alone to avoid unnecessary trades
- **Automatic Token Refresh** — GitHub Actions cron job refreshes Questrade OAuth tokens every 6 hours

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

### 4. Token Refresh Only (used by GitHub Actions)

```bash
python main.py --refresh-only
```

## Configuration

### Target Allocations (`config/targets.yaml`)

Edit target percentages (must sum to 100%):

```yaml
targets:
  CAD: 0.0      # Cash CAD
  USD: 0.0      # Cash USD
  VCN.TO: 2.0
  VUN.TO: 2.0
  IVV: 17.0
  XEF.TO: 2.0
  VSP.TO: 57.0
  XEC.TO: 6.0
  XBB.TO: 6.0
  CASH.TO: 8.0
```

### Placement Rules (`config/rules.yaml`)

Toggle rules on/off:

- **existing_positions_only** — Only trade positions that already exist in a given account
- **norberts_gambit** — Handle DLR.TO/DLR.U.TO as transient currency conversion instruments

## Project Structure

```
investment-rebalancer/
├── config/
│   ├── targets.yaml              # Target allocation percentages
│   └── rules.yaml                # Placement rules
├── tokens/
│   ├── jeff_token.json            # Jeff's Questrade refresh token
│   └── eunee_token.json           # Eunee's Questrade refresh token
├── src/
│   ├── questrade_client.py        # Questrade API wrapper
│   ├── portfolio.py               # Portfolio aggregation & accuracy
│   ├── rebalancer.py              # Core rebalancing logic
│   ├── rules.py                   # Trade placement rules engine
│   ├── currency.py                # USD/CAD exchange rate & Norbert's Gambit
│   └── display.py                 # Rich terminal output
├── .github/workflows/
│   └── refresh_tokens.yml         # 6hr token refresh cron
├── main.py                        # Entry point
└── requirements.txt
```

## GitHub Actions Token Refresh

The workflow in `.github/workflows/refresh_tokens.yml` runs every 6 hours to keep Questrade OAuth tokens fresh. The Questrade API goes offline periodically, so an issue is only created if refreshes fail for more than 48 consecutive hours.

**Before running locally:** Always `git pull` to get the latest refreshed tokens.

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