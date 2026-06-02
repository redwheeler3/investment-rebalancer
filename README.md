# Investment Rebalancer

A Python-based portfolio rebalancer for Questrade accounts that treats multiple accounts across multiple Questrade logins as one unified portfolio.

Built as an alternative to [Passiv](https://passiv.com/), this project combines portfolio rebalancing
with a public-code/private-state model that keeps expiring Questrade tokens
refreshed for free via GitHub Actions.

For Questrade specifically, local-only usage is not enough if you want this to be
hands-off. If the app does not run often enough, the refresh tokens eventually
expire. At that point you have to go back to the Questrade website, generate new
tokens manually, and copy them back into your setup. This architecture avoids
that recurring manual recovery step.

This project uses a simple pattern that works well for Questrade and can be
adapted to other token-based API workflows:

- keep the **code public**
- keep the **live state private**
- let **GitHub Actions refresh and persist the state for free**

In practice, that means the public repo contains the app, docs, and templates,
while a separate private repo holds the mutable state: tokens, real target
config, and portfolio history. The app reads that private state through one
explicit environment variable, `REBALANCER_STATE_DIR`, so local runs and GitHub
Actions both operate against the same source of truth.

> TL;DR: Keep code public and reusable, while keeping broker credentials and rotating state private and fully automated.

---

## Why use this?

Rebalancing keeps your portfolio aligned with the plan you chose on purpose. As
markets move, some assets grow faster than others, new cash accumulates, and your
actual allocation drifts. Left alone, that drift can quietly change your risk
level — for example, leaving you with more equity exposure, sector concentration,
or currency exposure than you intended.

Rebalancing also adds discipline. Instead of chasing whatever has recently done
well, you systematically trim assets that have become overweight and add to
assets that are underweight. In other words, it nudges you toward the classic
"sell high, buy low" behaviour while keeping the focus on your target allocation.

This project is for DIY investors who want to keep control of their own trades
without rebuilding a spreadsheet every time the portfolio drifts. It turns the
mechanical work into a repeatable report: measure the whole household portfolio,
compare it to your targets, identify what is overweight or underweight, and show
specific whole-share trades you can review before placing orders.

You still define the target allocation, drift threshold, account setup, and
tactical rules. The app handles the allocation math, account constraints,
CAD/USD planning, and Questrade-specific workflow details.

---

## Features

- **Unified Portfolio View** — Aggregates all accounts into one portfolio view
- **Target Allocation Tracking** — Compares holdings against configurable targets
- **Accuracy Score** — Single percentage showing how close the portfolio is to target
- **Smart Trade Placement** — Only recommends trades in accounts that already hold the position
- **Currency Handling** — Detects USD/CAD conversion needs and flags Norbert's Gambit status
- **Transient Symbols** — Temporarily exclude symbols such as `DLR.TO` / `DLR.U.TO` during Norbert's Gambit
- **Unknown Holdings** — Symbols not in targets are treated as implicit 0% targets and recommended for sale
- **Projected Accuracy** — Shows expected accuracy after recommended trades
- **Whole-Share Trading** — Uses whole shares only, with bid pricing for sells and ask pricing for buys
- **Iterative Algorithm** — Repeats Sell → Buy → Sweep rounds until positions are within tolerance
- **Configurable Drift Trade Threshold** — Only acts on positions that drift beyond your chosen threshold
- **Tolerance-Aware Status Display** — Marks symbols as `OK`, `OVER`, or `UNDER`
- **Conservative FX Funding** — Uses conservative DLR bid/ask math for Norbert's Gambit sizing
- **Tactical Defensive Deployment** — Drawdown-based regime system that deploys fixed-income assets into equities during market drops and rebuilds on recovery
- **Drift & Token Alerting** — GitHub Issues created automatically when accuracy drops below 95% or tokens fail for >48h, auto-closed on recovery
- **Automatic Portfolio Sync** — Designed to run with GitHub Actions from a private state repo

---

## Rebalancing rules

The rebalancer is intentionally opinionated. The current planner is built around
these portfolio rules:

1. **Treat all accounts as one household portfolio**
   - Drift is measured at the total-portfolio level, not per account.

2. **Sell overweight positions and buy underweight positions**
   - The planner starts with symbols whose drift is materially away from target.

3. **Use a drift threshold to avoid tiny starter trades**
   - The configured `drift_trade_threshold_pct` is used to decide whether a
     symbol is overweight or underweight enough to begin trading.
   - This threshold is mainly a trade-suppression rule, not a hard law of the
     portfolio.

4. **Minimize free cash whenever practical**
   - Once meaningful trades are already happening, the planner is allowed to use
     leftover cash more aggressively so cash does not remain stranded.
   - This can include buying a symbol that is merely the best eligible holding
     in an account, even if it is no longer globally underweight.

5. **Only buy symbols that already exist in the account**
   - An account's current holdings define its buyable universe.
   - This rule is one of the main constraints that shapes the planner.

6. **Prefer same-currency deployment before cross-currency deployment**
   - If an account has useful same-currency buys available, those come before a
     CAD/USD conversion.
   - Cross-currency funding is still used when needed, with conservative
     Norbert's Gambit math.

7. **Allow account-constrained cash deployment, then clean up globally**
   - If cash lands in an account that can only productively buy a limited set of
     existing holdings, the planner may fully deploy that cash there first.
   - If that creates excess exposure at the household level, the planner can
     later sell that excess from another account where the proceeds can be used
     for more useful underweight buys.

8. **Avoid obviously wasteful churn**
   - The planner tries to avoid creating trade patterns that simply undo each
     other without improving drift or reducing idle cash.

9. **Use whole-share, real-side pricing**
   - Sells use bid pricing.
   - Buys use ask pricing.
   - Trades are sized in whole shares only.

10. **Treat unknown symbols as implicit 0% targets**
    - If a holding is not in the target map, it is still eligible to be sold.

11. **Respect transient/excluded symbols**
    - Symbols such as `DLR.TO` / `DLR.U.TO` can be temporarily excluded from
      rebalancing while a Norbert's Gambit is in flight.

This rule set is meant to reflect the practical objective of the project:

- improve household drift toward target,
- keep idle cash low,
- respect account-level holding constraints,
- and make the trade plan realistic for manual execution.

---

## Sample output

```
  Syncing private state repo: /Users/you/Documents/investment-rebalancer-state
  Remote is up to date — no changes pulled
  Loading configuration...
  Connecting to Questrade...
  ✓ Alice connected
  ✓ Bob connected
  Fetching USD/CAD exchange rate...
  USD/CAD rate: 1.3591
  Building portfolio...
  Fetching market quotes (bid/ask)...
  Fetching DLR quotes...
  DLR.TO bid/ask: $13.79 / $13.79 | DLR.U.TO bid/ask: $10.15 / $10.15
  Calculating trades...

╔═══════════════════════════════════════════╗
║ PORTFOLIO REBALANCER  —  2026-05-04 00:47 ║
╚═══════════════════════════════════════════╝

  Accuracy Score:          97.6%  →  99.9%
  All-Time High:           $842,180.52  ▼ -$0.00 (-0.0%) (2026-05-02)
  Portfolio Value:         $842,180.52  ▲ +$0.00 (+0.0%)

╭─────────────────────── Year-to-Date Portfolio Value ───────────────────────╮
│ $842K |                                                                 ░░ │
│       |                                                                ░░░ │
│       |                                                             ░░ ░░░ │
│       |                                                             ░░ ░░░ │
│       |                                                         ░░  ░░░░░░ │
│ $832K |                                                         ░░ ░░░░░░░ │
│       |                                                         ░░░░░░░░░░ │
│       |                                                        ░░░░░░░░░░░ │
│       |                                                        ░░░░░░░░░░░ │
│ $822K |                                                        ░░░░░░░░░░░ │
│       |                                                        ░░░░░░░░░░░ │
│       |                                                        ░░░░░░░░░░░ │
│       |                                                        ░░░░░░░░░░░ │
│       |                                                        ░░░░░░░░░░░ │
│ $812K |                                                       ░░░░░░░░░░░░ │
│       +---------------+--------------+----------------+--------------+--   │
│        Jan 01       Feb 01         Mar 01           Apr 01         May 01  │
│                                                                            │
│ Latest $842,180.52                                                         │
│ Low $812,430.18                                                            │
│ High $842,180.52                                                           │
╰────────────────────────────────────────────────────────────────────────────╯

                  Portfolio Holdings
╭──────────┬────────┬────────────────┬───────────────╮
│ Symbol   │ Shares │          Price │   Value (CAD) │
├──────────┼────────┼────────────────┼───────────────┤
│ VFV      │    312 │      US$148.22 │    $62,812.41 │
│ XEF.TO   │  2,840 │         $38.42 │   $109,112.80 │
│ XEC.TO   │  1,620 │         $31.05 │    $50,301.00 │
│ ZAG.TO   │ 14,780 │         $10.85 │   $160,363.00 │
│ VUN.TO   │  3,182 │         $62.18 │   $197,816.76 │
│ XBB.TO   │  4,320 │         $28.94 │   $125,020.80 │
│ QQQ      │    146 │      US$512.30 │   $101,611.14 │
├──────────┼────────┼────────────────┼───────────────┤
│ Cash CAD │        │                │       $182.37 │
│ Cash USD │        │      US$612.40 │       $832.24 │
├──────────┼────────┼────────────────┼───────────────┤
│ Total    │        │ USD/CAD 1.3591 │   $842,180.52 │
╰──────────┴────────┴────────────────┴───────────────╯

                                Account Summary
╭───────┬────────┬──────────┬────────────┬──────────┬─────────────┬────────────╮
│ Owner │ Type   │ Number   │ Total (CAD)│ Cash CAD │    Cash USD │ Positions  │
├───────┼────────┼──────────┼────────────┼──────────┼─────────────┼────────────┤
│ Alice │ TFSA   │ 12345678 │ $298,410.… │   $42.18 │   US$612.40 │ 3 (VFV,    │
│       │        │          │            │          │             │  QQQ,      │
│       │        │          │            │          │             │  ZAG.TO)   │
│ Alice │ RRSP   │ 23456789 │ $241,520.… │   $68.32 │     US$0.00 │ 3 (XEF.TO, │
│       │        │          │            │          │             │  VUN.TO,   │
│       │        │          │            │          │             │  XBB.TO)   │
│ Bob   │ Margin │ 34567890 │ $302,250.… │   $71.87 │     US$0.00 │ 4 (VUN.TO, │
│       │        │          │            │          │             │  XEF.TO,   │
│       │        │          │            │          │             │  XEC.TO,   │
│       │        │          │            │          │             │  ZAG.TO)   │
├───────┼────────┼──────────┼────────────┼──────────┼─────────────┼────────────┤
│ Total │        │          │ $842,180.… │  $182.37 │   US$612.40 │ $842,180.… │
╰───────┴────────┴──────────┴────────────┴──────────┴─────────────┴────────────╯

           Current vs Target Allocation
╭─────────┬──────────┬───────────┬───────┬────────╮
│ Symbol  │ Target % │ Current % │ Drift │ Status │
├─────────┼──────────┼───────────┼───────┼────────┤
│ VFV     │     8.0% │      7.5% │ -0.5% │ UNDER  │
│ XEC.TO  │     6.0% │      6.0% │ +0.0% │   OK   │
│ ZAG.TO  │    19.0% │     19.0% │ +0.0% │   OK   │
│ XEF.TO  │    13.0% │     13.0% │ +0.0% │   OK   │
│ VUN.TO  │    23.0% │     23.5% │ +0.5% │   OK   │
│ QQQ     │    12.0% │     12.1% │ +0.1% │   OK   │
│ XBB.TO  │    15.0% │     14.8% │ -0.2% │   OK   │
├─────────┼──────────┼───────────┼───────┼────────┤
│ CAD     │     2.0% │      2.0% │ +0.0% │   OK   │
│ USD     │     2.0% │      2.1% │ +0.1% │   OK   │
╰─────────┴──────────┴───────────┴───────┴────────╯

                               Recommended Trades
╭─────────┬────────┬─────┬───────────┬─────────────┬─────────────┬─────────────╮
│ Symbol  │ Action │ Qty │     Price │  Est. Value │ Account     │ Note        │
├─────────┼────────┼─────┼───────────┼─────────────┼─────────────┼─────────────┤
│ VUN.TO  │  SELL  │  68 │    $62.18 │   $4,228.24 │ Bob Margin  │             │
│         │        │     │           │             │ (34567890)  │             │
│ VFV     │  BUY   │  28 │ US$148.22 │ US$4,150.16 │ Alice TFSA  │ Requires    │
│         │        │     │           │             │ (12345678)  │ currency    │
│         │        │     │           │             │             │ conversion  │
│ XBB.TO  │  BUY   │  42 │    $28.94 │   $1,215.48 │ Alice RRSP  │ Residual    │
│         │        │     │           │             │ (23456789)  │ cash        │
│         │        │     │           │             │             │ deployment  │
│ XEC.TO  │  BUY   │  18 │    $31.05 │     $558.90 │ Bob Margin  │ Residual    │
│         │        │     │           │             │ (34567890)  │ cash        │
│         │        │     │           │             │             │ deployment  │
╰─────────┴────────┴─────┴───────────┴─────────────┴─────────────┴─────────────╯

                    Currency Conversions (Norbert's Gambit)
╭─────────────────┬────────────┬────────┬────────┬───────────┬─────────────────╮
│ Account         │ Direction  │ Buy    │ Shares │ DLR Price │   Amount (incl. │
│                 │            │        │        │           │            fee) │
├─────────────────┼────────────┼────────┼────────┼───────────┼─────────────────┤
│ Alice TFSA      │ CAD -> USD │ DLR.TO │    302 │    $13.79 │   $4,174.07 CAD │
│ (12345678)      │            │        │        │           │  -> US$3,065.30 │
│                 │            │        │        │           │             USD │
╰─────────────────┴────────────┴────────┴────────┴───────────┴─────────────────╯

         Projected Allocation (After Trades)
╭─────────┬──────────┬─────────────┬───────┬────────╮
│ Symbol  │ Target % │ Projected % │ Drift │ Status │
├─────────┼──────────┼─────────────┼───────┼────────┤
│ VFV     │     8.0% │        8.0% │ +0.0% │   OK   │
│ XEC.TO  │     6.0% │        6.1% │ +0.1% │   OK   │
│ ZAG.TO  │    19.0% │       19.0% │ +0.0% │   OK   │
│ XEF.TO  │    13.0% │       13.0% │ +0.0% │   OK   │
│ VUN.TO  │    23.0% │       23.0% │ +0.0% │   OK   │
│ QQQ     │    12.0% │       12.1% │ +0.1% │   OK   │
│ XBB.TO  │    15.0% │       15.0% │ +0.0% │   OK   │
├─────────┼──────────┼─────────────┼───────┼────────┤
│ CAD     │     2.0% │        1.9% │ -0.1% │   OK   │
│ USD     │     2.0% │        1.9% │ -0.1% │   OK   │
╰─────────┴──────────┴─────────────┴───────┴────────╯

  ✓ Pushed updated private state to remote
```

The report shows your current drift, recommends specific trades to bring the
portfolio back toward target, and sizes any required Norbert's Gambit currency
conversions with DLR share counts.

---

## Why Norbert's Gambit is part of this workflow

Questrade users often prefer **Norbert's Gambit** for larger CAD/USD conversions
because it can be cheaper than a standard broker FX conversion. This project
does not execute the gambit for you, but it does model the planning around it:

- detecting when cross-currency funding is needed
- sizing the conversion conservatively with DLR bid/ask pricing
- accounting for the trading fee in the recommendation
- letting you mark `DLR.TO` / `DLR.U.TO` as transient while the gambit is in flight

That keeps the rebalance plan realistic without pretending that the conversion is
instant or free.

---

## Architecture

The model is easiest to understand when you look at both repos together:

```text
Public repo: investment-rebalancer/
├── config/
│   └── settings.example.yaml
├── data/
│   └── portfolio_history.example.jsonl
├── tokens/
│   └── token.example.json
├── templates/
│   └── private-state-repo/
│       ├── cleanup-runs.yml
│       └── portfolio_sync.yml
├── src/
├── main.py
└── requirements.txt

Private repo: investment-rebalancer-state/
├── .github/
│   └── workflows/
│       ├── cleanup-runs.yml
│       └── portfolio_sync.yml
├── config/
│   └── settings.yaml
├── data/
│   ├── portfolio_history.jsonl
│   ├── tactical_state.json       (created automatically on first regime change)
│   └── fx_targets_state.json     (created automatically on first FX target resolution)
└── tokens/
    ├── primary_token.json
    └── secondary_token.json
```

The public repo contains code, docs, examples, and workflow templates.
The private repo contains the live state the app reads through
`REBALANCER_STATE_DIR`.

### Source modules

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point — loads config, connects clients, orchestrates the run |
| `src/portfolio.py` | Data model (positions, accounts, holdings), allocation math, and trade projection |
| `src/rebalancer.py` | Core decision engine — decides what to trade and which accounts to use |
| `src/cash_deploy.py` | Residual cash deployment — builds buy trades from leftover cash |
| `src/models.py` | Shared data types (`TradeRecommendation`, `TransientAlert`) and constants |
| `src/fx_math.py` | Currency conversion math (Norbert's Gambit sizing, cross-currency capacity) |
| `src/fx_rate.py` | Live USD/CAD rate fetching and DLR quote retrieval |
| `src/fx_conversions.py` | Post-rebalance DLR trade planning (Norbert's Gambit execution) |
| `src/fx_targets.py` | Resolves FX-based target allocation rules from config |
| `src/report_builder.py` | Assembles all report data (trades, projections, history) for display |
| `src/display.py` | Terminal rendering with Rich (tables, charts, formatting) |
| `src/history.py` | Portfolio value history — ATH tracking, daily change, YTD chart data |
| `src/tactical.py` | Tactical defensive deployment — drawdown-based dynamic target adjustment |
| `src/questrade_client.py` | Questrade API client (OAuth token rotation, positions, quotes) |
| `src/paths.py` | Resolves private state repo paths from `REBALANCER_STATE_DIR` |

---

## Quick start

### 1. Clone the public code repo

```bash
git clone https://github.com/<you>/investment-rebalancer.git
cd investment-rebalancer
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create a private state repo

Create a separate private repository anywhere on your machine, for example:

```text
C:\Users\you\Documents\investment-rebalancer-state
```

or on macOS:

```text
/Users/you/Documents/investment-rebalancer-state
```

Create this structure inside it:

```text
investment-rebalancer-state/
├── config/
├── data/
└── tokens/
```

If you plan to use the GitHub Actions automation described below, you will also
add this later in the private repo:

```text
.github/
└── workflows/
```

### 4. Add your private settings

Copy the public example file:

- `config/settings.example.yaml` from this repo
- into `config/settings.yaml` in your private state repo

Then edit it with your real allocation targets and account definitions.

The same file also defines which Questrade logins to connect to. Instead of
hardcoding household-specific names or corporate labels in the public code,
the app now reads account definitions from private config.

Example shape:

```yaml
accounts:
  - owner_name: Primary
    token_file: primary_token.json
  - owner_name: Secondary
    token_file: secondary_token.json
    account_type_display_overrides:
      Corporation: HoldingCo
```

- `owner_name` is the label shown in reports
- `token_file` is the filename under `tokens/`
- `account_type_display_overrides` is optional and lets you relabel specific
  Questrade `clientAccountType` values for display purposes

If Questrade reports `clientAccountType: Corporation` for one of those logins,
you can map it to any display label you want, such as `HoldingCo`.

### 5. Add your Questrade token files

Create token files in the private repo:

```text
tokens/primary_token.json
tokens/secondary_token.json
```

Each file should look like:

```json
{
  "refresh_token": "YOUR_QUESTRADE_REFRESH_TOKEN"
}
```

You can use `tokens/token.example.json` in this repo as a template.

### 6. Optional: add portfolio history

If you want all-time high / daily change / YTD reporting immediately, create:

```text
data/portfolio_history.jsonl
```

You can start with an empty file, or copy the structure from `data/portfolio_history.example.jsonl`.

If the file doesn't exist yet, the app will create it when it first records a value.

### 7. Set `REBALANCER_STATE_DIR`

Point the environment variable at the root of your private state repo.

#### Windows - persistent user variable

In **Command Prompt**:

```cmd
setx REBALANCER_STATE_DIR "C:\Users\you\Documents\investment-rebalancer-state"
```

Then close and reopen your terminal or VS Code.

#### Windows - temporary for current shell

```cmd
set REBALANCER_STATE_DIR=C:\Users\you\Documents\investment-rebalancer-state
```

#### macOS / Linux - temporary for current shell

```bash
export REBALANCER_STATE_DIR="/Users/you/Documents/investment-rebalancer-state"
```

#### macOS / Linux - persistent

Add this to your shell profile such as `~/.zshrc` or `~/.bashrc`:

```bash
export REBALANCER_STATE_DIR="/Users/you/Documents/investment-rebalancer-state"
```

Then reload your shell:

```bash
source ~/.zshrc
```

### 8. Run the rebalancer

Once the environment variable is set, your normal command stays simple:

```bash
python main.py
```

---

## Private repo GitHub Actions setup

For automation, the app supports a `--sync` mode that refreshes all token files,
snapshots the current portfolio value, and exits — expecting the surrounding
workflow to commit and push the updated state.

This repo includes **workflow template files** you can copy into your private
state repo:

```text
templates/private-state-repo/portfolio_sync.yml
templates/private-state-repo/cleanup-runs.yml
```

Copy them into your private repo at:

```text
investment-rebalancer-state/
└── .github/
    └── workflows/
        ├── portfolio_sync.yml
        └── cleanup-runs.yml
```

Before using `portfolio_sync.yml`, open the copied file and replace:

```text
<PUBLIC_REPO_OWNER>/<PUBLIC_REPO_NAME>
```

with the actual GitHub owner/name of this public code repository, for example:

```text
redwheeler3/investment-rebalancer
```

What each workflow is for:

- `portfolio_sync.yml` — a template for the private repo's main sync workflow;
  after you replace the public repo owner/name, it can run on a schedule or
  manually, refresh tokens, snapshot portfolio value/history, and commit any
  updated private-state files back to the private repo
- `cleanup-runs.yml` — a template for a small maintenance workflow that runs
  monthly to delete old GitHub Actions workflow runs so the private repo's
  Actions history stays tidy

### How the private workflow works

1. Checks out the private state repo
2. Checks out this public code repo into a sibling folder
3. Sets `REBALANCER_STATE_DIR` to the private repo path
4. Runs `python main.py --sync`
5. Commits rotated tokens and updated history back to the private repo

This gives you automated token refresh without ever storing live credentials in the public repo.

---

## Tactical defensive deployment

The tactical system implements a modified 120-minus-age rule combined with a
drawdown-based deployment plan. At baseline, the portfolio holds 80% equities
and 20% fixed income. When the market drops, fixed-income assets are deployed
into equities at predefined thresholds. On recovery, the fixed position is
rebuilt.

### How it works

The system maintains a **Reference High** — the peak portfolio value used as
the anchor for all drawdown calculations. At baseline, this tracks the ATH
naturally. When deployment first triggers, the Reference High freezes and stays
fixed until the portfolio recovers back to baseline.

**Deployment thresholds** (going down from Reference High):

| Drawdown | Fixed | Equity | Regime |
|----------|-------|--------|--------|
| 0% (baseline) | 20% | 80% | Baseline |
| -10% | 15% | 85% | Level 1 |
| -20% | 10% | 90% | Level 2 |
| -30% | 5% | 95% | Level 3 |

**Recovery thresholds** (on the way back up):

| Recovery to | Fixed | Equity | Returns to |
|-------------|-------|--------|------------|
| -15% from reference | 10% | 90% | Level 2 |
| -5% from reference | 15% | 85% | Level 1 |
| +5% above reference | 20% | 80% | Baseline |

The different thresholds on the way down vs. up (hysteresis) prevent whipsawing
near boundaries.

### Fixed-income composition

The fixed allocation is split by configurable ratios across instruments:

```yaml
fixed_composition:
  ZMMK.TO: 50.0    # Money market (50% of fixed)
  XSH.TO: 25.0     # Canadian short-term corporate bonds (25% of fixed)
  XIGS.TO: 25.0    # US short-term corporate bonds (25% of fixed)
```

These ratios are maintained regardless of the total fixed percentage.

### Equity scaling

When the fixed allocation shrinks, all equity targets scale up proportionally.
For example, at Level 1 (85% equity, up from 80%), each equity target is
multiplied by 85/80 = 1.0625.

### Display

At baseline, the tactical system is invisible — nothing extra appears in the
report. When deployed, a panel appears showing the current regime, Reference
High, drawdown percentage, and dollar values for the next recovery and deploy
triggers.

### State persistence

Regime state is stored in `data/tactical_state.json` in the private state repo.
The file is only written on regime transitions (a few times per year at most).
The `--sync` mode also evaluates tactical transitions so GitHub Actions catches
daily threshold crossings.

### Configuration

Add a `tactical_deployment` section to `config/settings.yaml`:

```yaml
tactical_deployment:
  baseline_fixed_pct: 20.0
  fixed_composition:
    ZMMK.TO: 50.0
    XSH.TO: 25.0
    XIGS.TO: 25.0
  deploy_thresholds:
    - { drawdown_pct: -10.0, fixed_pct: 15.0 }
    - { drawdown_pct: -20.0, fixed_pct: 10.0 }
    - { drawdown_pct: -30.0, fixed_pct: 5.0 }
  recovery_thresholds:
    - { drawdown_pct: -15.0, fixed_pct: 10.0 }
    - { drawdown_pct: -5.0, fixed_pct: 15.0 }
    - { drawdown_pct: 5.0, fixed_pct: 20.0 }
```

Symbols listed in `fixed_composition` should **not** also appear in the static
`targets` — they are managed exclusively by the tactical system.

---

## Configuration

Your real configuration lives in `config/settings.yaml` in the private state
repo. Use `config/settings.example.yaml` in this public repo as the starting
point.

Key fields:

- `targets` — static target allocations (should sum to ~100% with FX and tactical rules)
- `accounts` — token filenames, display labels, and optional account-type overrides
- `fx_target_rules` — exchange-rate-driven target logic
- `tactical_deployment` — drawdown-based regime system for fixed/equity split (optional)
- `transient_symbols` — symbols to exclude temporarily from trading
- `norberts_gambit_fee_cad` — estimated fee used in conversion suggestions
- `drift_trade_threshold_pct` — minimum drift before the rebalancer acts

Any non-transient symbol you hold that isn't in `targets` gets an implicit 0%
target and will be recommended for sale.

---

## Security notes

- Never commit live Questrade tokens to the public repo
- Keep your real `settings.yaml` in the private state repo
- Keep `portfolio_history.jsonl` in the private state repo
- If you are converting an old private repo into a public one, do **not** rely only on deleting files from the latest commit — clean or replace the git history first

---

## Troubleshooting

### `REBALANCER_STATE_DIR is not set`

Set the environment variable and restart your terminal.

### `Private state repo is missing required directory 'tokens/'`

Create the expected folder structure in your private state repo:

```text
config/
data/
tokens/
```

### `Missing config file .../config/settings.yaml`

Copy `config/settings.example.yaml` from this repo into your private state repo as `config/settings.yaml`.

### Local run worked but push failed

Your tokens may have rotated locally without being pushed upstream. Push the private state repo manually before running from another machine or waiting for GitHub Actions.

---

## Accuracy score

The accuracy score is calculated as:

```text
Accuracy = 100% - (sum of absolute drifts / 2)
```

- **100%** = perfectly balanced
- **>95%** = green, very close to target
- **90-95%** = yellow, rebalance recommended
- **<90%** = red, rebalance is urgent

---

## License

This project is licensed under the **Apache License 2.0**. See [LICENSE](LICENSE).