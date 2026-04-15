"""
Investment Rebalancer — Main Entry Point

Connects to Questrade accounts, builds a unified portfolio view,
calculates rebalancing trades, and displays a formatted report.

Usage:
    python main.py                  # Run the rebalancer
    python main.py --refresh-only   # Just refresh tokens (used by GitHub Actions)
"""

import subprocess
import sys
import yaml
from pathlib import Path

# Ensure UTF-8 output on Windows (prevents UnicodeEncodeError with Rich)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from src.questrade_client import QuestradeClient, refresh_token_only
from src.portfolio import build_portfolio, freeze_symbols, fetch_quotes_for_holdings, get_current_allocations, calculate_accuracy, get_drifts
from src.rebalancer import calculate_trades, simulate_rebalance
from src.rules import get_transient_status
from src.currency import get_usd_to_cad_rate, fetch_dlr_quotes, calculate_currency_needs
from src.history import record_value, get_all_time_high
from src.display import display_full_report, console


# Paths
ROOT = Path(__file__).parent
TOKENS_DIR = ROOT / "tokens"
CONFIG_DIR = ROOT / "config"


def load_config() -> tuple:
    """Load target allocations, transient symbols, and fee from config/targets.yaml.

    Returns:
        Tuple of (targets dict, transient_symbols list, norberts_gambit_fee_cad float).
    """
    targets_path = CONFIG_DIR / "targets.yaml"
    with open(targets_path, "r") as f:
        data = yaml.safe_load(f)

    targets = data.get("targets", {})
    transient_symbols = data.get("transient_symbols", [])
    norberts_gambit_fee_cad = data.get("norberts_gambit_fee_cad", 10.49)

    # Validate targets sum to ~100%
    total = sum(targets.values())
    if abs(total - 100.0) > 0.5:
        console.print(f"  [yellow]⚠ Warning: Target allocations sum to {total:.1f}% (expected 100%)[/yellow]")

    return targets, transient_symbols, norberts_gambit_fee_cad


def refresh_tokens_only():
    """Refresh all tokens and record portfolio value. Used by GitHub Actions.

    Also snapshots the portfolio value for ATH tracking so that the
    daily cron job contributes data points even without a full rebalance.
    """
    token_files = list(TOKENS_DIR.glob("*_token.json"))

    if not token_files:
        print("ERROR: No token files found in tokens/")
        sys.exit(1)

    all_ok = True
    clients = []
    for token_file in token_files:
        name = token_file.stem.replace("_token", "")
        print(f"Refreshing token for {name}...")
        ok = refresh_token_only(str(token_file))
        if ok:
            print(f"  ✓ {name} token refreshed successfully")
            # Build a client from the freshly-refreshed token for portfolio snapshot
            try:
                clients.append(QuestradeClient(str(token_file), name))
            except Exception:
                pass  # Non-fatal — snapshot is best-effort
        else:
            print(f"  ✗ {name} token refresh FAILED")
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

    print("\nAll tokens refreshed successfully.")


def run_rebalancer():
    """Main rebalancer logic."""

    # Load config
    console.print("  [dim]Loading configuration...[/dim]")
    targets, transient_symbols, norberts_gambit_fee_cad = load_config()

    # Connect to Questrade accounts
    console.print("  [dim]Connecting to Questrade...[/dim]")
    clients = []

    for name, filename in [("Jeff", "jeff_token.json"), ("Eunee", "eunee_token.json")]:
        token_path = TOKENS_DIR / filename
        if token_path.exists():
            try:
                clients.append(QuestradeClient(str(token_path), name))
                console.print(f"  [green]✓[/green] {name} connected")
            except Exception as e:
                console.print(f"  [red]✗ {name} connection failed: {e}[/red]")

    if not clients:
        console.print("[red]ERROR: No Questrade connections established. Check your tokens.[/red]")
        sys.exit(1)

    # Get exchange rate (uses Questrade market data for real-time accuracy)
    console.print("  [dim]Fetching USD/CAD exchange rate...[/dim]")
    usd_to_cad_rate = get_usd_to_cad_rate(client=clients[0])
    console.print(f"  [dim]USD/CAD rate: {usd_to_cad_rate:.4f}[/dim]")

    # Build portfolio
    console.print("  [dim]Building portfolio...[/dim]")
    portfolio = build_portfolio(clients, usd_to_cad_rate)

    # Fetch bid/ask quotes for accurate pricing (bid for sells, ask for buys)
    console.print("  [dim]Fetching market quotes (bid/ask)...[/dim]")
    fetch_quotes_for_holdings(portfolio, clients)

    # Calculate initial allocations and drift
    current_allocations = get_current_allocations(portfolio, usd_to_cad_rate)
    drifts = get_drifts(current_allocations, targets)

    # Exclude transient symbols from rebalancing.
    # Their value stays in total_value_cad so allocation math stays correct.
    transient_status = get_transient_status(portfolio, transient_symbols)
    if transient_status["symbols"]:
        freeze_symbols(portfolio, transient_status["symbols"])
        # Recompute after freezing (value stays in total, so other
        # allocations shift slightly — this is correct)
        current_allocations = get_current_allocations(portfolio, usd_to_cad_rate)
        drifts = get_drifts(current_allocations, targets)

    accuracy = calculate_accuracy(current_allocations, targets)
    transient_alerts = transient_status["alerts"]

    # Fetch DLR quotes for Norbert's Gambit calculations (single API call)
    console.print("  [dim]Fetching DLR quotes...[/dim]")
    dlr = fetch_dlr_quotes(clients[0])
    dlr_price = dlr.cad_price
    if dlr_price > 0:
        console.print(f"  [dim]DLR.TO price: ${dlr_price:.2f}[/dim]")
    else:
        console.print("  [yellow]Could not fetch DLR.TO price — DLR share counts will be unavailable[/yellow]")

    # Calculate trades (transient symbols are excluded)
    console.print("  [dim]Calculating trades...[/dim]")
    trades = calculate_trades(portfolio, targets, usd_to_cad_rate, norberts_gambit_fee_cad, existing_only=True, transient_symbols=transient_status["symbols"])

    # Calculate currency conversion needs (per-account with DLR share counts)
    currency_conversions = calculate_currency_needs(trades, portfolio.accounts, usd_to_cad_rate, dlr_price, norberts_gambit_fee_cad)

    # Simulate projected accuracy after trades
    projected_accuracy = None
    projected_allocations = None
    if trades:
        simulation = simulate_rebalance(portfolio, trades, targets, usd_to_cad_rate)
        projected_accuracy = simulation["projected_accuracy"]
        projected_allocations = simulation["projected_allocations"]

    # Record portfolio value and check all-time high
    record_value(portfolio.total_value_cad)
    ath = get_all_time_high(current_value=portfolio.total_value_cad)

    # Display the report
    display_full_report(
        portfolio=portfolio,
        current_allocations=current_allocations,
        targets=targets,
        drifts=drifts,
        accuracy=accuracy,
        trades=trades,
        currency_conversions=currency_conversions,
        transient_alerts=transient_alerts,
        usd_to_cad_rate=usd_to_cad_rate,
        projected_accuracy=projected_accuracy,
        projected_allocations=projected_allocations,
        all_time_high=ath,
    )


def _push_synced_files():
    """Commit and push tokens + portfolio history so GitHub Actions stays in sync.
    Questrade uses single-use refresh tokens (token rotation), so every run
    invalidates the old token. Portfolio history is also pushed so ATH
    tracking is shared between local runs and GitHub Actions."""
    try:
        # Check if there are changes to push (tokens or data)
        result = subprocess.run(
            ["git", "diff", "--quiet", "tokens/", "data/"],
            cwd=str(ROOT), capture_output=True,
        )
        # Also check for untracked files in data/ (first run)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard", "data/"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if result.returncode == 0 and not untracked.stdout.strip():
            return  # No changes

        subprocess.run(
            ["git", "add", "tokens/", "data/"],
            cwd=str(ROOT), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "🔄 Auto-sync tokens and portfolio history"],
            cwd=str(ROOT), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=str(ROOT), check=True, capture_output=True,
        )
    except FileNotFoundError:
        console.print("\n  [yellow]⚠ git not found — remember to push files manually[/yellow]")
    except subprocess.CalledProcessError:
        console.print("\n  [yellow]⚠ Could not auto-push — remember to push manually[/yellow]")


def main():
    """Entry point."""
    if "--refresh-only" in sys.argv:
        refresh_tokens_only()
    else:
        run_rebalancer()
    _push_synced_files()


if __name__ == "__main__":
    main()