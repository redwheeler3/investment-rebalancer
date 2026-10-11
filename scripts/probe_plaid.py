"""Search Plaid's Canadian directory and optionally inspect Hosted Link.

Run: python scripts/probe_plaid.py --environment production
Credentials come from hidden terminal prompts or PLAID_CLIENT_ID/PLAID_SECRET.
"""

import argparse
import getpass
import os
import sys
from urllib.parse import urlparse
from uuid import uuid4
import warnings
import webbrowser

import requests


def get_credential(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        if not sys.stdin.isatty():
            raise RuntimeError(f"Run in an interactive terminal to enter {name} privately.")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            try:
                value = getpass.getpass(f"{name}: ").strip()
            except getpass.GetPassWarning:
                raise RuntimeError("This terminal cannot hide credential input.") from None
    if not value:
        raise RuntimeError(f"{name} is required.")
    return value


def request_plaid(environment: str, client_id: str, secret: str, endpoint: str, payload: dict):
    response = requests.post(
        f"https://{environment}.plaid.com{endpoint}",
        headers={
            "PLAID-CLIENT-ID": client_id,
            "PLAID-SECRET": secret,
            "Plaid-Version": "2020-09-14",
        },
        json=payload,
        timeout=30,
        allow_redirects=False,
    )
    # Keep response bodies and HTTP exception details out of diagnostic output.
    if not response.ok or response.is_redirect:
        raise RuntimeError(f"Plaid {endpoint} failed with HTTP {response.status_code}.")
    return response.json()


def inspect_link(environment: str, client_id: str, secret: str, queries: list[str]):
    data = request_plaid(
        environment, client_id, secret, "/link/token/create",
        {
            "client_name": "Investment Rebalancer connection inspection",
            "country_codes": ["CA"],
            "language": "en",
            "user": {"client_user_id": str(uuid4())},
            "products": ["transactions"],
            "hosted_link": {},
        },
    )
    url = data["hosted_link_url"]
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "secure.plaid.com"
            or not parsed.path.startswith("/hl/")):
        raise RuntimeError("Plaid returned an unexpected Hosted Link address.")
    print("\nOpening Plaid Hosted Link for inspection.")
    print(f"Search for: {', '.join(queries)}. Inspect the login instructions and field labels.")
    print("Close the flow before entering bank credentials or completing a connection.")
    print("Share only the institution name and login field labels; keep the session URL private.")
    if not webbrowser.open_new_tab(url):
        print(f"Open this private session URL in your browser: {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", choices=["production", "sandbox"], required=True)
    parser.add_argument(
        "--query", nargs="+", default=["Saven", "FirstOntario"],
        help="Institution names to search (default: Saven FirstOntario).",
    )
    parser.add_argument(
        "--inspect-link", action="store_true",
        help="Open the official Plaid connection screen for inspection without bank login.",
    )
    args = parser.parse_args()
    print(f"Plaid environment: {args.environment}")
    if args.environment == "sandbox":
        print("Sandbox results cannot establish real bank connectivity.")
    try:
        client_id = get_credential("PLAID_CLIENT_ID")
        secret = get_credential("PLAID_SECRET")
        for query in args.query:
            data = request_plaid(
                args.environment, client_id, secret, "/institutions/search",
                {
                    "query": query,
                    "country_codes": ["CA"],
                    "products": None,
                    "options": {"include_optional_metadata": True},
                },
            )
            institutions = data["institutions"]
            print(f"\n{query}: {len(institutions)} matching institutions")
            for institution in institutions:
                print(f"  {institution['name']} ({institution['institution_id']})")
                print(f"  Website: {institution.get('url') or 'not supplied'}")
                print(f"  Products: {', '.join(institution.get('products', []))}")
                print(f"  OAuth: {institution.get('oauth', 'not supplied')}")
        print("\nDirectory matches do not prove that a bank login will connect.")
        if args.inspect_link:
            inspect_link(args.environment, client_id, secret, args.query)
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
    except (requests.RequestException, ValueError, KeyError, TypeError):
        print("Plaid request or response failed; no credentials were logged.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
