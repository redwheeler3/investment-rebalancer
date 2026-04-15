"""
Investment Rebalancer — Main Entry Point

Connects to Questrade accounts, builds a unified portfolio view,
calculates rebalancing trades, and displays a formatted report.

Usage:
    python main.py                  # Run the rebalancer
    python main.py --refresh-only   # Just refresh tokens (used by GitHub Actions)
"""

import sys
import os
import yaml
from pathlib import Path

# Ensure UTF-8 output on Windows (prevents UnicodeEncodeError with Rich)
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

from src.questrade_client import QuestradeClient, refresh_token_only
from src.portfolio import build_portfolio, fetch_quotes_for_holdings, get_current_allocations, calculate_accuracy, get_drifts
from src.rebalancer import calculate_trades, simulate_rebalance
from src.rules import check_transient_holdings
from src.currency import get_usd_to_cad_rate, get_dlr_price, calculate_currency_needs
from src.display import display_full_report, console


# Paths
ROOT = Path(__file__).parent
TOKENS_DIR = ROOT / "tokens"
CONFIG_DIR = ROOT / "config"


def load_targets() -> dict:
    """Load target allocations from config/targets.yaml."""
    targets_path = CONFIG_DIR / "targets.yaml"
    with open(targets_path, "r") as f:
        data = yaml.safe_load(f)

    targets = data.get("targets", {})

    # Validate targets sum to ~100%
    total = sum(targets.values())
    if abs(total - 100.0) > 0.5:
        console.print(f"  [yellow]⚠ Warning: Target allocations sum to {total:.1f}% (expected 100%)[/yellow]")

    return targets


def load_rules() -> dict:
    """Load placement rules from config/rules.yaml."""
    rules_path = CONFIG_DIR / "rules.yaml"
    with open(rules_path, "r") as f:
        data = yaml.safe_load(f)
    return data


def refresh_tokens_only():
    """Refresh all tokens without running the rebalancer. Used by GitHub Actions."""
    token_files = list(TOKENS_DIR.glob("*_token.json"))

    if not token_files:
        print("ERROR: No token files found in tokens/")
        sys.exit(1)

    all_ok = True
    for token_file in token_files:
        name = token_file.stem.replace("_token", "")
        print(f"Refreshing token for {name}...")
        ok = refresh_token_only(str(token_file))
        if ok:
            print(f"  ✓ {name} token refreshed successfully")
        else:
            print(f"  ✗ {name} token refresh FAILED")
            all_ok = False

    if not all_ok:
        sys.exit(1)

    print("\nAll tokens refreshed successfully.")


def run_rebalancer():
    """Main rebalancer logic."""

    # Load config
    console.print("  [dim]Loading configuration...[/dim]")
    targets = load_targets()
    rules_config = load_rules()

    # Determine which rules are enabled
    rules = {r["name"]: r["enabled"] for r in rules_config.get("rules", [])}
    transient_symbols = rules_config.get("transient_symbols", [])
    existing_only = rules.get("existing_positions_only", True)

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
    portfolio = build_portfolio(clients, transient_symbols, usd_to_cad_rate)

    # Fetch bid/ask quotes for accurate pricing (bid for sells, ask for buys)
    console.print("  [dim]Fetching market quotes (bid/ask)...[/dim]")
    fetch_quotes_for_holdings(portfolio, clients)

    # Calculate allocations and drift
    current_allocations = get_current_allocations(portfolio, usd_to_cad_rate)
    drifts = get_drifts(current_allocations, targets)
    accuracy = calculate_accuracy(current_allocations, targets)

    # Check for transient holdings
    transient_alerts = check_transient_holdings(portfolio.accounts, transient_symbols)

    # Fetch DLR.TO price for Norbert's Gambit calculations
    console.print("  [dim]Fetching DLR.TO price...[/dim]")
    dlr_price = get_dlr_price(clients[0])
    if dlr_price > 0:
        console.print(f"  [dim]DLR.TO price: ${dlr_price:.2f}[/dim]")
    else:
        console.print("  [yellow]Could not fetch DLR.TO price — DLR share counts will be unavailable[/yellow]")

    # Calculate trades
    console.print("  [dim]Calculating trades...[/dim]")
    trades = calculate_trades(portfolio, targets, usd_to_cad_rate, existing_only, transient_symbols)

    # Calculate currency conversion needs (per-account with DLR share counts)
    currency_conversions = calculate_currency_needs(trades, portfolio.accounts, usd_to_cad_rate, dlr_price)

    # Simulate projected accuracy after trades
    projected_accuracy = None
    projected_allocations = None
    if trades:
        simulation = simulate_rebalance(portfolio, trades, targets, usd_to_cad_rate)
        projected_accuracy = simulation["projected_accuracy"]
        projected_allocations = simulation["projected_allocations"]

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
    )


def main():
    """Entry point."""
    if "--refresh-only" in sys.argv:
        refresh_tokens_only()
    else:
        run_rebalancer()


if __name__ == "__main__":
    main()