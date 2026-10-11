"""Inspect a Saven CSV balance and optionally save an encrypted private snapshot."""

import argparse
from pathlib import Path
import sys

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.encrypted_state import (
    read_encrypted_json,
    write_encrypted_json,
)
from src.paths import get_config_dir, get_data_dir
from src.saven_tracking import parse_saven_account_numbers, refresh_saven_balances


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--directory", type=Path)
    parser.add_argument("--account-number", help="Refresh only one of the configured accounts.")
    parser.add_argument("--save", action="store_true", help="Save with SOPS in the private state repo.")
    args = parser.parse_args()
    try:
        with (get_config_dir() / "settings.yaml").open(encoding="utf-8") as stream:
            settings = yaml.safe_load(stream)
        if not isinstance(settings, dict):
            raise ValueError("The portfolio configuration must be a mapping.")
        account_numbers = parse_saven_account_numbers(settings)
        if args.account_number is not None and args.account_number not in account_numbers:
            raise ValueError("The requested account is not in saven.account_numbers.")
        if not account_numbers:
            print("No Saven accounts are configured for tracking.")
            return 0
        if args.directory is not None and not args.directory.is_dir():
            raise ValueError("The export directory does not exist.")
        destination = get_data_dir() / "saven_balance.json"
        existing = read_encrypted_json(destination) if destination.exists() else {}
        selected_numbers = [args.account_number] if args.account_number is not None else account_numbers
        selected_snapshot, refreshed = refresh_saven_balances(
            selected_numbers, existing, args.directory, args.file,
        )
        snapshot = {"accounts": {
            number: balance for number, balance in existing.get("accounts", {}).items()
            if number in account_numbers
        }}
        snapshot["accounts"].update(selected_snapshot["accounts"])
        for number, balance in selected_snapshot["accounts"].items():
            print(f"Account ending: {number[-4:]}")
            print(f"Reported balance: CAD ${float(balance['balance_cad']):,.2f}")
            print(f"Latest transaction date: {balance['last_transaction_date']}")
            print(f"Source file modified (UTC): {balance['source_modified_at']}")
            print(f"Source filename: {balance['source_filename']}")
            print(f"Status: {'new export' if number in refreshed else 'cached balance'}")
        print("Compare this reported balance with Saven's current balance before saving.")
        print("The export does not identify its selected date-range cutoff.")
        if args.save and (refreshed or snapshot != existing):
            write_encrypted_json(destination, snapshot)
            print(f"Encrypted snapshot saved: {destination}")
        elif args.save:
            print("The saved balances are unchanged.")
        else:
            print("Inspection only. Add --save after verifying the balance.")
        print("The snapshot is not yet consumed by the portfolio report.")
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError, yaml.YAMLError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
