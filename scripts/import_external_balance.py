"""Inspect configured external-account exports and optionally save encrypted state."""

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.encrypted_state import read_encrypted_json
from src.external_accounts import (
    parse_external_accounts,
    refresh_external_balances,
    save_external_balances,
)
from src.external_providers import PROVIDERS, provider_for
from src.paths import get_config_dir, get_data_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--directory", type=Path)
    parser.add_argument(
        "--account", help="Refresh one configured PROVIDER:<account-id>."
    )
    parser.add_argument(
        "--save", action="store_true", help="Save encrypted state in the private repo."
    )
    args = parser.parse_args()
    try:
        with (get_config_dir() / "settings.yaml").open(encoding="utf-8") as stream:
            settings = yaml.safe_load(stream)
        if not isinstance(settings, dict):
            raise ValueError("The portfolio configuration must be a mapping.")
        accounts = parse_external_accounts(settings)
        if args.account is not None and args.account not in accounts:
            raise ValueError("The requested account is not in the portfolio targets.")
        if not accounts:
            print("No external accounts are configured for tracking.")
            return 0
        if args.directory is not None and not args.directory.is_dir():
            raise ValueError("The export directory does not exist.")
        selected = {args.account: accounts[args.account]} if args.account else accounts
        groups = {}
        for symbol, config in selected.items():
            groups.setdefault(provider_for(symbol).key, []).append(config.identifier)
        for key, identifiers in groups.items():
            provider = PROVIDERS[key]
            destination = get_data_dir() / provider.cache_filename
            existing = read_encrypted_json(destination) if destination.exists() else {}
            exports = {}
            chosen, refreshed = refresh_external_balances(
                provider,
                identifiers,
                existing,
                args.directory,
                args.file,
                on_export=lambda identifier, exported: exports.update(
                    {identifier: exported}
                ),
            )
            tracked = {
                config.identifier
                for symbol, config in accounts.items()
                if provider_for(symbol).key == key
            }
            snapshot = {
                "accounts": {
                    identifier: balance
                    for identifier, balance in existing.get("accounts", {}).items()
                    if identifier in tracked
                }
            }
            snapshot["accounts"].update(chosen["accounts"])
            for identifier, balance in chosen["accounts"].items():
                config = accounts[f"{key}:{identifier}"]
                print(
                    f"{config.display_name} {config.account_type} (...{identifier[-4:]})"
                )
                print(f"Reported balance: CAD ${float(balance['balance_cad']):,.2f}")
                print(f"Latest activity date: {balance['last_transaction_date']}")
                print(f"Source modified (UTC): {balance['source_modified_at']}")
                print(
                    f"Status: {'new export' if identifier in refreshed else 'cached balance'}"
                )
            if args.save:
                if save_external_balances(
                    provider, destination, snapshot, existing, exports, set(accounts)
                ):
                    print(f"Encrypted snapshot saved: {destination}")
                else:
                    print("The saved balances are unchanged.")
        print(
            "Verify reported balances against the institutions; filtered exports can omit newer activity."
        )
        if not args.save:
            print("Inspection only. Add --save after verifying the balances.")
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError, yaml.YAMLError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
