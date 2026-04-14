"""
Questrade API client for authentication and data retrieval.

Handles OAuth token refresh, account listing, positions, balances, and market quotes.
"""

import json
import os
import requests
from pathlib import Path


QUESTRADE_AUTH_URL = "https://login.questrade.com/oauth2/token"


class QuestradeClient:
    """Client for interacting with the Questrade API."""

    def __init__(self, token_path: str, owner_name: str):
        """
        Initialize the Questrade client.

        Args:
            token_path: Path to the JSON file containing the refresh token.
            owner_name: Friendly name for this account holder (e.g., "Jeff", "Eunee").
        """
        self.token_path = Path(token_path)
        self.owner_name = owner_name
        self.access_token = None
        self.api_server = None
        self.refresh_token = None
        self._load_and_authenticate()

    def _load_and_authenticate(self):
        """Load refresh token from file and authenticate with Questrade."""
        with open(self.token_path, "r") as f:
            data = json.load(f)

        self.refresh_token = data["refresh_token"]
        self._authenticate()

    def _authenticate(self):
        """Exchange refresh token for access token and new refresh token."""
        resp = requests.get(
            QUESTRADE_AUTH_URL,
            params={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            },
            timeout=30,
        )
        resp.raise_for_status()
        token_data = resp.json()

        self.access_token = token_data["access_token"]
        self.api_server = token_data["api_server"]
        self.refresh_token = token_data["refresh_token"]

        # Save the new refresh token back to file
        self._save_refresh_token()

    def _save_refresh_token(self):
        """Persist the new refresh token to the token file."""
        with open(self.token_path, "w") as f:
            json.dump({"refresh_token": self.refresh_token}, f, indent=2)

    def _headers(self) -> dict:
        """Return authorization headers for API requests."""
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, endpoint: str, params: dict = None) -> dict:
        """
        Make a GET request to the Questrade API.

        Args:
            endpoint: API endpoint path (e.g., "/v1/accounts").
            params: Optional query parameters.

        Returns:
            JSON response as a dictionary.
        """
        url = f"{self.api_server}{endpoint}"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_accounts(self) -> list:
        """
        Get all accounts under this login.

        Returns:
            List of account dictionaries with keys like 'number', 'type', 'status'.
        """
        data = self._get("v1/accounts")
        return data.get("accounts", [])

    def get_positions(self, account_id: str) -> list:
        """
        Get positions for a specific account.

        Args:
            account_id: The Questrade account number.

        Returns:
            List of position dictionaries.
        """
        data = self._get(f"v1/accounts/{account_id}/positions")
        return data.get("positions", [])

    def get_balances(self, account_id: str) -> dict:
        """
        Get balances for a specific account.

        Args:
            account_id: The Questrade account number.

        Returns:
            Dictionary with balance information including cash.
        """
        data = self._get(f"v1/accounts/{account_id}/balances")
        return data

    def get_quote(self, symbol_ids: list) -> list:
        """
        Get market quotes for a list of symbol IDs.

        Args:
            symbol_ids: List of Questrade internal symbol IDs.

        Returns:
            List of quote dictionaries.
        """
        if not symbol_ids:
            return []
        ids_str = ",".join(str(sid) for sid in symbol_ids)
        data = self._get(f"v1/markets/quotes", params={"ids": ids_str})
        return data.get("quotes", [])

    def search_symbol(self, symbol: str) -> list:
        """
        Search for a symbol to get its Questrade symbol ID.

        Args:
            symbol: Ticker symbol (e.g., "VSP.TO").

        Returns:
            List of matching symbol dictionaries.
        """
        data = self._get("v1/symbols/search", params={"prefix": symbol})
        return data.get("symbols", [])

    def get_symbol_info(self, symbol_id: int) -> dict:
        """
        Get detailed information about a symbol.

        Args:
            symbol_id: Questrade internal symbol ID.

        Returns:
            Symbol information dictionary.
        """
        data = self._get(f"v1/symbols/{symbol_id}")
        return data.get("symbols", [{}])[0]


def refresh_token_only(token_path: str) -> bool:
    """
    Refresh the token without doing anything else.
    Used by the GitHub Actions cron job.

    Args:
        token_path: Path to the token JSON file.

    Returns:
        True if refresh was successful, False otherwise.
    """
    try:
        with open(token_path, "r") as f:
            data = json.load(f)

        resp = requests.get(
            QUESTRADE_AUTH_URL,
            params={
                "grant_type": "refresh_token",
                "refresh_token": data["refresh_token"],
            },
            timeout=30,
        )
        resp.raise_for_status()
        token_data = resp.json()

        with open(token_path, "w") as f:
            json.dump({"refresh_token": token_data["refresh_token"]}, f, indent=2)

        return True
    except Exception as e:
        print(f"ERROR refreshing token at {token_path}: {e}")
        return False