# Investment Rebalancer

A Python-based portfolio rebalancer for Questrade accounts. It replaces Passiv with a more customizable workflow that treats multiple accounts across multiple Questrade logins as one unified portfolio.

## Why this repo is structured differently

This project intentionally separates:

- **public code** — this repository
- **private personal state** — a separate private repo that you control

That private state repo contains:

- your live Questrade refresh tokens
- your real `targets.yaml`
- your portfolio history file

This is one of the best parts of the implementation:

> you can keep the code public and reusable, while keeping broker credentials and rotating state private and still fully automated.

Questrade refresh tokens rotate and expire quickly, so they should never live in a public repo. Instead, this app reads state from a private companion repo through one explicit environment variable:

```bash
REBALANCER_STATE_DIR
```

If that variable is missing, the app fails fast with a clear error.

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

## Architecture

### Public repo

This repo contains:

- application code
- documentation
- public-safe examples
- private-state workflow templates

It does **not** contain:

- live token files
- real target allocations
- real portfolio history

### Private state repo

Your separate private repo should contain:

```text
investment-rebalancer-state/
├── config/
│   └── targets.yaml
├── data/
│   └── portfolio_history.json
└── tokens/
    ├── jeff_token.json
    └── eunee_token.json
```

The app reads all mutable state from that directory.

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

### 4. Add your target config

Copy the public example file:

- `config/targets.example.yaml` from this repo
- into `config/targets.yaml` in your private state repo

Then edit it with your real allocation targets.

### 5. Add your Questrade token files

Create token files in the private repo:

```text
tokens/jeff_token.json
tokens/eunee_token.json
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
data/portfolio_history.json
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

The recommended automation model is:

- **public repo** holds the code
- **private repo** holds tokens, config, history, and scheduled workflows

This repo includes workflow templates you can copy into your private state repo:

```text
templates/private-state-repo/portfolio_sync.yml
templates/private-state-repo/cleanup-runs.yml
```

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
config/targets.yaml
```

Use `config/targets.example.yaml` in this public repo as the starting point.

Key fields:

- `targets` — static target allocations
- `fx_target_rules` — exchange-rate-driven target logic
- `transient_symbols` — symbols to exclude temporarily from trading
- `norberts_gambit_fee_cad` — estimated fee used in conversion suggestions
- `drift_trade_threshold_pct` — minimum drift before the rebalancer acts

### Notes

- **Transient symbols:** useful for DLR.TO / DLR.U.TO during Norbert's Gambit
- **Unknown holdings:** any non-transient symbol missing from targets gets an implicit 0% target
- **Target totals:** static targets plus enabled FX-derived targets should sum to about 100%

---

## Project structure

```text
investment-rebalancer/
├── config/
│   └── targets.example.yaml
├── data/
│   └── portfolio_history.example.jsonl
├── tokens/
│   └── token.example.json
├── templates/
│   └── private-state-repo/
│       ├── cleanup-runs.yml
│       └── portfolio_sync.yml
├── src/
│   ├── questrade_client.py
│   ├── history.py
│   ├── paths.py
│   └── ...
├── main.py
└── requirements.txt
```

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
- Keep your real `targets.yaml` in the private state repo if you consider it personal data
- Keep `portfolio_history.json` in the private state repo
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

### `Missing config file .../config/targets.yaml`

Copy `config/targets.example.yaml` from this repo into your private state repo as `config/targets.yaml`.

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