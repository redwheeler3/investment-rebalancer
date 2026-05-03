# Investment Rebalancer

A Python-based portfolio rebalancer for Questrade accounts that treats multiple accounts across multiple Questrade logins as one unified portfolio.

Built as an alternative to Passiv, this project combines portfolio rebalancing
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

> Keep code public and reusable, while keeping broker credentials and rotating state private and fully automated.

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
- **Sell Trimming Reconciliation** — Trims excess sells when possible
- **Automatic Portfolio Sync** — Designed to run with GitHub Actions from a private state repo

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
│   └── portfolio_history.jsonl
└── tokens/
    ├── primary_token.json
    └── secondary_token.json
```

The public repo contains code, docs, examples, and workflow templates.
The private repo contains the live state the app reads through
`REBALANCER_STATE_DIR`.

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

## Scheduled sync mode

For automation, the app also supports:

```bash
python main.py --sync
```

This mode:

- refreshes all token files in the private state repo
- snapshots the current portfolio value for history / ATH tracking
- expects the surrounding GitHub Actions workflow to commit and push the updated private state

---

## Private repo GitHub Actions setup

The recommended automation model is simple:

- **public repo** = code and public-safe examples
- **private repo** = your state and scheduled workflows

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

## Configuration

Your real configuration lives in the private state repo at:

```text
config/settings.yaml
```

Use `config/settings.example.yaml` in this public repo as the starting point.

Key fields:

- `targets` — static target allocations
- `accounts` — token filenames, display labels, and optional account-type overrides
- `fx_target_rules` — exchange-rate-driven target logic
- `transient_symbols` — symbols to exclude temporarily from trading
- `norberts_gambit_fee_cad` — estimated fee used in conversion suggestions
- `drift_trade_threshold_pct` — minimum drift before the rebalancer acts

### Notes

- **Transient symbols:** useful for DLR.TO / DLR.U.TO during Norbert's Gambit
- **Unknown holdings:** any non-transient symbol missing from targets gets an implicit 0% target
- **Target totals:** static targets plus enabled FX-derived targets should sum to about 100%
- **Account labels:** the `accounts:` section in private config controls token filenames,
  report labels, and optional account-type display overrides

---

## Local sync behavior

When you run:

```bash
python main.py
```

the app will:

1. `git pull --ff-only` in your private state repo
2. run the rebalancer
3. refresh any rotated tokens
4. update portfolio history
5. commit and push `tokens/` and `data/` in the private state repo

That keeps:

- your Windows machine
- your Mac
- and your private GitHub Actions workflow

all aligned on the latest token state.

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
- **98%+** = very close to target
- **95-98%** = minor drift, probably fine
- **<95%** = rebalance recommended