"""
Investment Rebalancer — Main Entry Point

Connects to Questrade accounts, builds a unified portfolio view,
calculates rebalancing trades, and displays a formatted report.

Usage:
    python main.py          # Run the rebalancer interactively
    python main.py --sync   # Scheduled sync mode: refresh tokens + snapshot portfolio (used by GitHub Actions)
"""

import argparse
import math
import os
import subprocess
import sys
from pathlib import Path

import requests
import yaml

# Ensure UTF-8 output on Windows (prevents UnicodeEncodeError with Rich)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from src.display import console, display_full_report
from src.external_accounts import (
    ExternalAccountConfig,
    attach_external_balances,
    load_external_balances,
    parse_external_accounts,
    resolve_external_transfer_accounts,
)
from src.fixed_income import fixed_income_budget
from src.fx_conversions import calculate_currency_needs
from src.fx_rate import MarketRateUnavailable, fetch_dlr_quotes, get_usd_to_cad_rate
from src.fx_targets import resolve_targets
from src.history import get_all_time_high, record_value, household_date
from src.models import get_transient_status
from src.paths import get_config_dir, get_state_root, get_tokens_dir
from src.portfolio import (
    build_allocation_snapshot,
    build_portfolio,
    fetch_quotes_for_holdings,
    simulate_rebalance,
)
from src.questrade_client import QuestradeClient, RETRYABLE_HTTP_STATUSES
from src.rebalancer import (
    calculate_trades,
    gic_purchase_amounts,
    redemption_suggestions,
)
from src.redemption import parse_redemption_holding_months
from src.report_builder import build_report_data
from src.tactical import (
    evaluate_tactical_posture,
    parse_tactical_config,
    resolve_tactical_targets,
)
from src.state_sync import StateSyncError, prepare_state, sync_state
from src.transfer_tracking import reconcile_transfer_activity


def load_config() -> tuple:
    """Load config data from config/settings.yaml.

    Returns:
        Tuple of (
            accounts list,
            targets dict,
            transient_symbols list,
            norberts_gambit_fee_cad float,
            fx_target_rules dict,
            drift_trade_threshold_pct float,
            redemption_rules dict,
            tactical_config,
            external_accounts dict,
        ).
    """
    targets_path = get_config_dir() / "settings.yaml"
    if not targets_path.exists():
        raise FileNotFoundError(
            f"Missing config file at '{targets_path}'. "
            "Create it in your private state repo. See README.md for the required layout."
        )

    with open(targets_path, "r") as f:
        data = yaml.safe_load(f)

    if "redemption_rules" in data:
        raise ValueError(
            "Replace redemption_rules with redemption_holding_months in settings.yaml "
            "(for example, use 24 for a two-year holding period)."
        )

    raw_accounts = data.get("accounts", [])
    if not raw_accounts:
        raise ValueError(
            "No accounts configured in config/settings.yaml. "
            "Add at least one account with owner_name and token_file."
        )

    accounts = []
    for index, account in enumerate(raw_accounts, start=1):
        owner_name = str(account.get("owner_name", "")).strip()
        token_file = str(account.get("token_file", "")).strip()
        overrides = account.get("account_type_display_overrides", {}) or {}

        if not owner_name or not token_file:
            raise ValueError(
                f"Account #{index} must define both owner_name and token_file in config/settings.yaml."
            )

        if Path(token_file).name != token_file or not token_file.endswith(".json"):
            raise ValueError("token_file must be a JSON filename inside tokens/.")
        if any(a["token_file"].casefold() == token_file.casefold() for a in accounts):
            raise ValueError("Each login requires a distinct token file.")
        accounts.append(
            {
                "owner_name": owner_name,
                "token_file": token_file,
                "account_type_display_overrides": {
                    str(key): str(value) for key, value in overrides.items()
                },
            }
        )

    targets = data.get("targets", {})
    for symbol, weight in targets.items():
        if symbol in {"CAD", "USD"}:
            raise ValueError(
                f"Remove {symbol} from targets. Brokerage cash always targets zero."
            )
        if type(weight) not in (int, float) or not math.isfinite(weight) or weight < 0:
            raise ValueError(f"Invalid target weight for {symbol}.")
    transient_symbols = data.get("transient_symbols", [])
    norberts_gambit_fee_cad = data.get("norberts_gambit_fee_cad", 0.0)
    fx_target_rules = data.get("fx_target_rules", {})
    drift_trade_threshold_pct = data.get("drift_trade_threshold_pct", 0.1)
    for name, value in (
        ("norberts_gambit_fee_cad", norberts_gambit_fee_cad),
        ("drift_trade_threshold_pct", drift_trade_threshold_pct),
    ):
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite nonnegative number.")
    tactical_config = parse_tactical_config(data.get("tactical_deployment", {}))
    external_accounts = parse_external_accounts(data)
    equity_symbols = set(targets)
    for rule in fx_target_rules.values():
        pair = {rule.get("usd_symbol"), rule.get("cad_symbol")}
        if pair & {"CAD", "USD"}:
            raise ValueError("Brokerage cash cannot be an FX target instrument.")
        equity_symbols.update(pair)
    if tactical_config and equity_symbols.intersection(
        tactical_config.fixed_composition
    ):
        raise ValueError(
            "Tactical fixed holdings must appear only in fixed_composition."
        )
    # Tactical scaling must not hide a misconfigured baseline allocation.
    baseline_total = sum(targets.values()) + sum(
        float(rule["total_target_pct"]) for rule in fx_target_rules.values()
    )
    if tactical_config:
        baseline_total += tactical_config.baseline_fixed_pct
    if not math.isfinite(baseline_total) or abs(baseline_total - 100) > 0.5:
        raise ValueError("Configured baseline allocations must sum to 100%.")

    return (
        accounts,
        targets,
        transient_symbols,
        norberts_gambit_fee_cad,
        fx_target_rules,
        drift_trade_threshold_pct,
        parse_redemption_holding_months(data.get("redemption_holding_months", {})),
        tactical_config,
        external_accounts,
    )


def _validate_resolved_targets(targets: dict):
    """Fail fast if the final resolved target map is materially off 100%."""
    if any(not math.isfinite(v) or v < 0 for v in targets.values()):
        raise ValueError("Resolved target weights must be finite and nonnegative.")
    total = sum(targets.values())
    if abs(total - 100.0) > 0.5:
        raise ValueError(
            f"Resolved target allocations sum to {total:.2f}% (expected 100%). "
            "Check config/settings.yaml static targets plus any fx_target_rules."
        )


def run_scheduled_sync():
    """Scheduled sync mode used by GitHub Actions.

    Refreshes all Questrade OAuth tokens (single rotation per token file)
    and snapshots the current portfolio value for ATH tracking.
    Also evaluates tactical regime transitions when tactical deployment is configured.
    Computes and outputs portfolio accuracy for drift alerting.
    Each token is published immediately. GitHub Actions publishes remaining state.
    """
    (
        accounts,
        targets,
        transient_symbols,
        _norberts_gambit_fee_cad,
        fx_target_rules,
        _drift_trade_threshold_pct,
        redemption_rules,
        tactical_config,
        external_accounts,
    ) = load_config()
    prepare_state(a["token_file"] for a in accounts)
    # Missing/corrupt external balances must fail before history or tactical state.
    external_balances = load_external_balances(external_accounts)

    all_ok = True
    clients = []
    tokens_dir = get_tokens_dir()
    for account in accounts:
        name = account["owner_name"]
        token_file = tokens_dir / account["token_file"]
        print(f"Refreshing token for {name}...")
        try:
            # QuestradeClient.__init__ refreshes and persists the rotated token.
            client = QuestradeClient(
                str(token_file),
                name,
                account_type_display_overrides=account[
                    "account_type_display_overrides"
                ],
            )
            clients.append(client)
            print(f"  ✓ {name} token refreshed successfully")
        except StateSyncError:
            raise
        except Exception as e:  # noqa: BLE001 - refresh every account before reporting failures
            print(f"  ✗ {name} token refresh FAILED: {e}")
            all_ok = False

    if not all_ok:
        sys.exit(1)

    as_of = household_date()
    try:
        usd_to_cad_rate = get_usd_to_cad_rate(client=clients[0])
    except (MarketRateUnavailable, requests.Timeout, requests.ConnectionError) as error:
        print(f"  ⚠ Could not snapshot portfolio value: {error}")
        return
    except requests.HTTPError as error:
        if getattr(error.response, "status_code", None) not in RETRYABLE_HTTP_STATUSES:
            raise
        print(f"  ⚠ Could not snapshot portfolio value: {error}")
        return
    portfolio = build_portfolio(clients, usd_to_cad_rate, redemption_rules)
    attach_external_balances(portfolio, external_balances, external_accounts)
    transfer_accounts = resolve_external_transfer_accounts(portfolio, external_accounts)
    reconcile_transfer_activity(
        portfolio, external_balances, clients, transfer_accounts
    )
    record_value(portfolio.total_value_cad, as_of=as_of)
    print(f"  ✓ Portfolio value recorded: ${portfolio.total_value_cad:,.2f}")

    # Evaluate tactical regime transitions (if configured)
    if tactical_config:
        ath = get_all_time_high(current_value=portfolio.total_value_cad, as_of=as_of)
        posture = evaluate_tactical_posture(
            current_value=portfolio.total_value_cad,
            ath_value=ath.value,
            ath_date=ath.date,
            config=tactical_config,
            as_of=as_of,
        )
        if posture.transition_occurred:
            print(
                f"  ⚡ Tactical regime changed: "
                f"{posture.previous_regime} → {posture.regime}"
            )
        else:
            print(f"  ✓ Tactical regime: {posture.regime}")

    # Compute accuracy for drift alerting
    resolved_targets = resolve_targets(targets, fx_target_rules, usd_to_cad_rate)
    if tactical_config:
        resolved_targets = resolve_tactical_targets(
            resolved_targets, posture, tactical_config
        )
    _validate_resolved_targets(resolved_targets)
    hidden_symbols = {
        symbol for symbol in transient_symbols if symbol in portfolio.holdings
    }
    snapshot = build_allocation_snapshot(
        portfolio,
        resolved_targets,
        usd_to_cad_rate,
        excluded_symbols=hidden_symbols,
    )
    accuracy = snapshot.accuracy
    print(f"  ✓ Accuracy: {accuracy:.1f}%")

    # Write accuracy to GitHub Actions output (if running in CI)
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"accuracy={accuracy:.1f}\n")

    print("\nPortfolio sync complete.")


def _connect_clients(accounts: list[dict]) -> list:
    """Connect to every configured Questrade account or fail closed."""
    console.print("  [dim]Connecting to Questrade...[/dim]")
    clients = []
    failed_accounts = []
    tokens_dir = get_tokens_dir()

    for account in accounts:
        name = account["owner_name"]
        token_path = tokens_dir / account["token_file"]
        if not token_path.exists():
            console.print(
                f"  [red]✗ {name} connection failed: token file not found[/red]"
            )
            failed_accounts.append(name)
            continue

        try:
            clients.append(
                QuestradeClient(
                    str(token_path),
                    name,
                    account_type_display_overrides=account[
                        "account_type_display_overrides"
                    ],
                )
            )
            console.print(f"  [green]✓[/green] {name} connected")
        except StateSyncError:
            raise
        except Exception as e:  # noqa: BLE001 - attempt every configured account before failing
            console.print(f"  [red]✗ {name} connection failed: {e}[/red]")
            failed_accounts.append(name)

    if failed_accounts:
        console.print(
            "[red]ERROR: Rebalancing requires every configured Questrade account. "
            f"Unavailable: {', '.join(failed_accounts)}. No portfolio state was evaluated.[/red]"
        )
        sys.exit(1)

    return clients


def _build_priced_portfolio(clients: list, usd_to_cad_rate: float, redemption_rules):
    """Build the portfolio and enrich it with bid/ask quote data."""
    console.print("  [dim]Building portfolio...[/dim]")
    portfolio = build_portfolio(clients, usd_to_cad_rate, redemption_rules)

    console.print("  [dim]Fetching market quotes and previous close...[/dim]")
    fetch_quotes_for_holdings(portfolio, clients)
    return portfolio


def _fetch_dlr_quotes(client):
    """Fetch and display the DLR quotes used for Norbert's Gambit calculations."""
    console.print("  [dim]Fetching DLR quotes...[/dim]")
    dlr_quotes = fetch_dlr_quotes(client)
    if dlr_quotes.cad_buy_price > 0 or dlr_quotes.usd_buy_price > 0:
        console.print(
            "  [dim]DLR.TO bid/ask: "
            f"${dlr_quotes.cad_sell_price:.2f} / ${dlr_quotes.cad_buy_price:.2f}"
            " | DLR.U.TO bid/ask: "
            f"${dlr_quotes.usd_sell_price:.2f} / ${dlr_quotes.usd_buy_price:.2f}[/dim]"
        )
    else:
        console.print(
            "  [yellow]Could not fetch DLR quotes — DLR share counts will be unavailable[/yellow]"
        )
    return dlr_quotes


def _render_report(
    portfolio,
    targets: dict,
    usd_to_cad_rate: float,
    drift_trade_threshold_pct: float,
    report,
    *,
    hidden_symbols: set,
    blocked_sales: set[tuple[str, str, str | None]],
) -> None:
    """Render the completed report data to the terminal."""
    projected_snapshot = report.projected

    display_full_report(
        portfolio=portfolio,
        current_allocations=report.current.allocations,
        targets=targets,
        accuracy=report.current.accuracy,
        trades=report.trades,
        currency_conversions=report.currency_conversions,
        redemption_reviews=redemption_suggestions(
            portfolio,
            report,
            usd_to_cad_rate,
            drift_trade_threshold_pct,
            hidden_symbols,
            blocked_sales=blocked_sales,
        ),
        transient_alerts=report.transient_alerts,
        usd_to_cad_rate=usd_to_cad_rate,
        projected_accuracy=projected_snapshot.accuracy if projected_snapshot else None,
        projected_allocations=projected_snapshot.allocations
        if projected_snapshot
        else None,
        all_time_high=report.all_time_high,
        ytd_history=report.ytd_history,
        drift_trade_threshold_pct=drift_trade_threshold_pct,
        tactical_posture=report.tactical_posture,
    )


def _display_external_status(
    account: ExternalAccountConfig, status: str, last_activity: str
) -> None:
    """Report the export lookup outcome and the activity date of the balance in use."""
    style, label = {
        "found": ("green", "Export found"),
        "unchanged": ("dim", "Export unchanged; cached balance"),
        "absent": ("dim", "Export absent; cached balance"),
        "older": ("yellow", "older export ignored; cached balance"),
        "cached": ("dim", "cached balance"),
    }[status]
    console.print(
        f"  [{style}]{account.display_name} …{account.identifier[-4:]}: {label}; last activity {last_activity}[/{style}]"
    )


def run_rebalancer():
    """Main rebalancer logic."""

    as_of = household_date()
    console.print("  [dim]Loading configuration...[/dim]")
    (
        accounts,
        targets,
        transient_symbols,
        norberts_gambit_fee_cad,
        fx_target_rules,
        drift_trade_threshold_pct,
        redemption_rules,
        tactical_config,
        external_accounts,
    ) = load_config()

    prepare_state(a["token_file"] for a in accounts)
    external_balances = load_external_balances(
        external_accounts,
        Path.home() / "Downloads",
        on_status=lambda symbol, status, activity: _display_external_status(
            external_accounts[symbol], status, activity
        ),
    )
    clients = _connect_clients(accounts)
    dlr_quotes = _fetch_dlr_quotes(clients[0])
    usd_to_cad_rate = dlr_quotes.exchange_rate
    if usd_to_cad_rate is None:
        raise RuntimeError(
            "Could not derive a live USD/CAD exchange rate from Questrade DLR quotes."
        )
    console.print(f"  [dim]USD/CAD rate: {usd_to_cad_rate:.4f}[/dim]")
    resolved_targets = resolve_targets(targets, fx_target_rules, usd_to_cad_rate)
    portfolio = _build_priced_portfolio(clients, usd_to_cad_rate, redemption_rules)
    attach_external_balances(portfolio, external_balances, external_accounts)
    transfer_accounts = resolve_external_transfer_accounts(portfolio, external_accounts)
    transfer_ledger = reconcile_transfer_activity(
        portfolio, external_balances, clients, transfer_accounts
    )
    # Evaluate tactical posture and adjust targets if configured
    tactical_posture = None
    if tactical_config:
        ath = get_all_time_high(current_value=portfolio.total_value_cad, as_of=as_of)
        tactical_posture = evaluate_tactical_posture(
            current_value=portfolio.total_value_cad,
            ath_value=ath.value,
            ath_date=ath.date,
            config=tactical_config,
            as_of=as_of,
        )
        resolved_targets = resolve_tactical_targets(
            resolved_targets, tactical_posture, tactical_config
        )

    _validate_resolved_targets(resolved_targets)

    transient_status = get_transient_status(portfolio, transient_symbols)
    hidden_symbols = transient_status["symbols"]
    transient_alerts = transient_status["alerts"]

    console.print("  [dim]Calculating trades...[/dim]")
    fixed_budget = fixed_income_budget(
        portfolio,
        resolved_targets,
        tactical_config.fixed_composition if tactical_config else {},
        usd_to_cad_rate,
    )
    if fixed_budget and fixed_budget.locked_excess_cad >= 0.01:
        console.print(
            "  [yellow]Locked fixed income exceeds the tactical budget by "
            f"CAD ${fixed_budget.locked_excess_cad:,.2f}.[/yellow]"
        )
    blocked_sales = set()
    trades = calculate_trades(
        portfolio,
        resolved_targets,
        usd_to_cad_rate,
        norberts_gambit_fee_cad,
        drift_trade_threshold_pct,
        transient_symbols=hidden_symbols,
        dlr_quotes=dlr_quotes,
        external_transfer_accounts=transfer_accounts,
        blocked_sales=blocked_sales,
        fixed_budget=fixed_budget,
        tactical_config=tactical_config,
    )
    fx_targets = resolved_targets
    if fixed_budget:
        purchases = gic_purchase_amounts(trades)
        if purchases:
            fixed_budget = fixed_income_budget(
                portfolio,
                resolved_targets,
                tactical_config.fixed_composition,
                usd_to_cad_rate,
                purchases,
            )
        # A residual FX sweep needs a holding that can still accept a purchase.
        projected = simulate_rebalance(
            portfolio,
            trades,
            resolved_targets,
            usd_to_cad_rate,
            dlr_quotes=dlr_quotes,
            fee_cad=norberts_gambit_fee_cad,
        )
        limits = fixed_budget.buy_limits(projected, portfolio.holdings, usd_to_cad_rate)
        fx_targets = resolved_targets | fixed_budget.targets
        for symbol, holding in portfolio.holdings.items():
            if holding.locked_bond_target or limits.get(symbol) == 0:
                fx_targets[symbol] = 0
    currency_conversions = calculate_currency_needs(
        trades,
        portfolio.accounts,
        usd_to_cad_rate,
        dlr_quotes,
        fx_targets,
        norberts_gambit_fee_cad,
    )
    report = build_report_data(
        portfolio,
        resolved_targets,
        usd_to_cad_rate,
        trades,
        currency_conversions,
        transient_alerts,
        hidden_symbols,
        tactical_posture=tactical_posture,
        as_of=as_of,
    )
    record_value(portfolio.total_value_cad, as_of=as_of)
    _render_report(
        portfolio,
        resolved_targets,
        usd_to_cad_rate,
        drift_trade_threshold_pct,
        report,
        hidden_symbols=hidden_symbols,
        blocked_sales=blocked_sales,
    )
    if transfer_ledger is not None:
        # Arm only recommendations the local user has seen in a successful report.
        # Scheduled reconciliation consumes saved intent without creating it.
        transfer_ledger.remember_recommendations(trades, transfer_accounts)
        transfer_ledger.save()


def _pull_latest():
    """Pull the latest private state from remote before running locally.
    Uses --ff-only so it only applies clean fast-forwards; exits on failure."""
    state_root = get_state_root()
    console.print(f"  [dim]Syncing private state repo: {state_root}[/dim]")
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(state_root),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            if "Already up to date." in result.stdout:
                console.print("  [dim]Remote is up to date — no changes pulled[/dim]")
            else:
                console.print(
                    "  [green]✓[/green] [dim]Pulled latest changes from remote[/dim]"
                )
        else:
            stderr = result.stderr.strip()
            console.print(
                "[red]ERROR: Could not pull latest private state.\n"
                "  Your working tree may have uncommitted changes, or the remote is unreachable.\n"
                f"  git: {stderr}[/red]"
            )
            sys.exit(1)
    except FileNotFoundError:
        console.print("[red]ERROR: git is not installed or not in PATH.[/red]")
        sys.exit(1)


def main():
    """Refresh state on schedule or display the interactive report."""
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--sync", action="store_true", help="Refresh tokens and record a snapshot"
    )
    if parser.parse_args().sync:
        run_scheduled_sync()
    else:
        _pull_latest()
        run_rebalancer()
        sync_state()


if __name__ == "__main__":
    main()
