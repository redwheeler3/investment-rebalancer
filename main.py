"""
Investment Rebalancer — Main Entry Point

Connects to Questrade accounts, builds a unified portfolio view,
calculates rebalancing trades, and displays a formatted report.

Usage:
    python main.py          # Run the rebalancer interactively
    python main.py --sync   # Scheduled sync mode: refresh tokens + snapshot portfolio (used by GitHub Actions)
"""

import subprocess
import sys
import yaml

# Ensure UTF-8 output on Windows (prevents UnicodeEncodeError with Rich)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from src.questrade_client import QuestradeClient
from src.portfolio import build_portfolio, fetch_quotes_for_holdings
from src.report_builder import build_report_data
from src.display import display_full_report, console
from src.fx_rate import get_usd_to_cad_rate, fetch_dlr_quotes
from src.fx_conversions import calculate_currency_needs
from src.history import record_value
from src.paths import get_config_dir, get_state_root, get_tokens_dir
from src.fx_targets import resolve_targets
from src.models import get_transient_status
from src.rebalancer import calculate_trades


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

        accounts.append({
            "owner_name": owner_name,
            "token_file": token_file,
            "account_type_display_overrides": {
                str(key): str(value)
                for key, value in overrides.items()
            },
        })

    targets = data.get("targets", {})
    transient_symbols = data.get("transient_symbols", [])
    norberts_gambit_fee_cad = data.get("norberts_gambit_fee_cad", 10.49)
    fx_target_rules = data.get("fx_target_rules", {})
    drift_trade_threshold_pct = float(data.get("drift_trade_threshold_pct", 0.1))

    return (
        accounts,
        targets,
        transient_symbols,
        norberts_gambit_fee_cad,
        fx_target_rules,
        drift_trade_threshold_pct,
    )


def _validate_resolved_targets(targets: dict):
    """Fail fast if the final resolved target map is materially off 100%."""
    total = sum(targets.values())
    if abs(total - 100.0) > 0.5:
        raise ValueError(
            f"Resolved target allocations sum to {total:.2f}% (expected 100%). "
            "Check config/settings.yaml static targets plus any enabled fx_target_rules."
        )


def run_scheduled_sync():
    """Scheduled sync mode used by GitHub Actions.

    Refreshes all Questrade OAuth tokens (single rotation per token file)
    and snapshots the current portfolio value for ATH tracking.
    GitHub Actions handles committing and pushing the updated files.
    """
    (
        accounts,
        _targets,
        _transient_symbols,
        _norberts_gambit_fee_cad,
        _fx_target_rules,
        _drift_trade_threshold_pct,
    ) = load_config()

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
                account_type_display_overrides=account["account_type_display_overrides"],
            )
            clients.append(client)
            print(f"  ✓ {name} token refreshed successfully")
        except Exception as e:
            print(f"  ✗ {name} token refresh FAILED: {e}")
            all_ok = False

    if not all_ok:
        sys.exit(1)

    # Snapshot portfolio value for ATH tracking (best-effort)
    if clients:
        try:
            usd_to_cad_rate = get_usd_to_cad_rate(client=clients[0])
            portfolio = build_portfolio(clients, usd_to_cad_rate)
            record_value(portfolio.total_value_cad)
            print(f"  ✓ Portfolio value recorded: ${portfolio.total_value_cad:,.2f}")
        except Exception as e:
            print(f"  ⚠ Could not snapshot portfolio value: {e}")

    print("\nPortfolio sync complete.")


def _connect_clients(accounts: list[dict]) -> list:
    """Connect to all configured Questrade accounts."""
    console.print("  [dim]Connecting to Questrade...[/dim]")
    clients = []
    tokens_dir = get_tokens_dir()

    for account in accounts:
        name = account["owner_name"]
        token_path = tokens_dir / account["token_file"]
        if token_path.exists():
            try:
                clients.append(QuestradeClient(
                    str(token_path),
                    name,
                    account_type_display_overrides=account["account_type_display_overrides"],
                ))
                console.print(f"  [green]✓[/green] {name} connected")
            except Exception as e:
                console.print(f"  [red]✗ {name} connection failed: {e}[/red]")

    if not clients:
        console.print(
            "[red]ERROR: No Questrade connections established. "
            "Check the tokens in your private state repo.[/red]"
        )
        sys.exit(1)

    return clients


def _fetch_exchange_rate(client) -> float:
    """Fetch and display the live USD/CAD exchange rate."""
    console.print("  [dim]Fetching USD/CAD exchange rate...[/dim]")
    usd_to_cad_rate = get_usd_to_cad_rate(client=client)
    console.print(f"  [dim]USD/CAD rate: {usd_to_cad_rate:.4f}[/dim]")
    return usd_to_cad_rate


def _build_priced_portfolio(clients: list, usd_to_cad_rate: float):
    """Build the portfolio and enrich it with bid/ask quote data."""
    console.print("  [dim]Building portfolio...[/dim]")
    portfolio = build_portfolio(clients, usd_to_cad_rate)

    console.print("  [dim]Fetching market quotes (bid/ask)...[/dim]")
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
        console.print("  [yellow]Could not fetch DLR quotes — DLR share counts will be unavailable[/yellow]")
    return dlr_quotes


def _render_report(
    portfolio,
    targets: dict,
    usd_to_cad_rate: float,
    drift_trade_threshold_pct: float,
    report,
) -> None:
    """Render the completed report data to the terminal."""
    projected_snapshot = report.projected

    display_full_report(
        portfolio=portfolio,
        current_allocations=report.current.allocations,
        targets=targets,
        drifts=report.current.drifts,
        accuracy=report.current.accuracy,
        trades=report.trades,
        currency_conversions=report.currency_conversions,
        transient_alerts=report.transient_alerts,
        usd_to_cad_rate=usd_to_cad_rate,
        projected_accuracy=projected_snapshot.accuracy if projected_snapshot else None,
        projected_allocations=projected_snapshot.allocations if projected_snapshot else None,
        all_time_high=report.all_time_high,
        daily_change=report.daily_change,
        ytd_history=report.ytd_history,
        drift_trade_threshold_pct=drift_trade_threshold_pct,
    )


def run_rebalancer():
    """Main rebalancer logic."""

    console.print("  [dim]Loading configuration...[/dim]")
    (
        accounts,
        targets,
        transient_symbols,
        norberts_gambit_fee_cad,
        fx_target_rules,
        drift_trade_threshold_pct,
    ) = load_config()

    clients = _connect_clients(accounts)
    usd_to_cad_rate = _fetch_exchange_rate(clients[0])
    resolved_targets = resolve_targets(targets, fx_target_rules, usd_to_cad_rate)
    _validate_resolved_targets(resolved_targets)
    portfolio = _build_priced_portfolio(clients, usd_to_cad_rate)
    dlr_quotes = _fetch_dlr_quotes(clients[0])

    transient_status = get_transient_status(portfolio, transient_symbols)
    hidden_symbols = transient_status["symbols"]
    transient_alerts = transient_status["alerts"]

    console.print("  [dim]Calculating trades...[/dim]")
    trades = calculate_trades(
        portfolio,
        resolved_targets,
        usd_to_cad_rate,
        norberts_gambit_fee_cad,
        drift_trade_threshold_pct,
        transient_symbols=hidden_symbols,
        dlr_quotes=dlr_quotes,
    )
    currency_conversions = calculate_currency_needs(
        trades,
        portfolio.accounts,
        usd_to_cad_rate,
        dlr_quotes,
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
    )
    record_value(portfolio.total_value_cad)
    _render_report(
        portfolio,
        resolved_targets,
        usd_to_cad_rate,
        drift_trade_threshold_pct,
        report,
    )


def _pull_latest():
    """Pull the latest private state from remote before running locally.
    Uses --ff-only so it only applies clean fast-forwards; warns and continues on failure."""
    state_root = get_state_root()
    console.print(f"  [dim]Syncing private state repo: {state_root}[/dim]")
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=str(state_root), capture_output=True, text=True,
        )
        if result.returncode == 0:
            if "Already up to date." in result.stdout:
                console.print("  [dim]Remote is up to date — no changes pulled[/dim]")
            else:
                console.print("  [green]✓[/green] [dim]Pulled latest changes from remote[/dim]")
        else:
            console.print("  [yellow]⚠ Could not sync with remote — running with local data[/yellow]")
    except FileNotFoundError:
        console.print("  [dim]git not found — skipping remote sync[/dim]")


def _push_synced_files():
    """Commit and push tokens + portfolio history so local runs and automation stay in sync.
    Questrade uses single-use refresh tokens (token rotation), so every run
    invalidates the old token. Portfolio history is also pushed so ATH
    tracking is shared between local runs and GitHub Actions."""
    state_root = get_state_root()
    try:
        # Check if there are changes to push (tokens or data)
        result = subprocess.run(
            ["git", "diff", "--quiet", "tokens/", "data/"],
            cwd=str(state_root), capture_output=True,
        )
        # Also check for untracked files in data/ (first run)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "data/"],
            cwd=str(state_root), capture_output=True, text=True,
        )
        if result.returncode == 0 and not untracked.stdout.strip():
            console.print("\n  [dim]No local changes to push[/dim]")
            return

        subprocess.run(
            ["git", "add", "tokens/", "data/"],
            cwd=str(state_root), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "🔄 Auto-sync tokens and portfolio history"],
            cwd=str(state_root), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=str(state_root), check=True, capture_output=True,
        )
        console.print("\n  [green]✓[/green] [dim]Pushed updated private state to remote[/dim]")
    except FileNotFoundError:
        console.print("\n  [yellow]⚠ git not found — remember to push your private state repo manually[/yellow]")
    except subprocess.CalledProcessError:
        console.print("\n  [yellow]⚠ Could not auto-push — remember to push your private state repo manually[/yellow]")


def main():
    """Entry point."""
    if "--sync" in sys.argv:
        # Scheduled sync mode (private repo GitHub Actions):
        # refresh tokens + snapshot portfolio. The private repo workflow
        # handles its own git commit/push,
        # so we don't call _push_synced_files() here.
        run_scheduled_sync()
    else:
        _pull_latest()
        run_rebalancer()
        _push_synced_files()


if __name__ == "__main__":
    main()