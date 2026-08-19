"""
Questrade API client for authentication and data retrieval.

Handles OAuth token refresh, account listing, positions, balances, and market quotes.
"""

import time
from pathlib import Path

import requests

from src.encrypted_state import read_encrypted_json, write_encrypted_json

QUESTRADE_AUTH_URL = "https://login.questrade.com/oauth2/token"
GET_RETRY_DELAYS_SECONDS = (0.5, 1.0)
RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


class QuestradeClient:
    """Client for interacting with the Questrade API."""

    def __init__(
        self,
        token_path: str,
        owner_name: str,
        account_type_display_overrides: dict,
    ):
        """
        Initialize the Questrade client.

        Args:
            token_path: Path to the JSON file containing the refresh token.
            owner_name: Friendly name for this account holder.
            account_type_display_overrides: Mapping of Questrade
                clientAccountType -> display owner label.
        """
        self.token_path = Path(token_path)
        self.owner_name = owner_name
        self.account_type_display_overrides = account_type_display_overrides
        self.access_token = None
        self.api_server = None
        self.refresh_token = None
        self._load_and_authenticate()

    def _load_and_authenticate(self):
        """Load refresh token from file and authenticate with Questrade."""
        data = read_encrypted_json(self.token_path)
        self.refresh_token = data["refresh_token"]
        self._authenticate()

    def _authenticate(self):
        """Exchange refresh token for access token and new refresh token."""
        resp = requests.post(
            QUESTRADE_AUTH_URL,
            data={
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
        """Persist the new refresh token atomically as SOPS ciphertext."""
        write_encrypted_json(self.token_path, {"refresh_token": self.refresh_token})

    def _headers(self) -> dict:
        """Return authorization headers for API requests."""
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, endpoint: str, params: dict | None = None) -> dict:
        """
        Make a GET request to the Questrade API.

        Args:
            endpoint: API endpoint path (e.g., "/v1/accounts").
            params: Optional query parameters.

        Returns:
            JSON response as a dictionary.
        """
        refreshed_auth = False
        transient_retries = 0

        while True:
            url = f"{self.api_server}{endpoint}"
            try:
                resp = requests.get(
                    url,
                    headers=self._headers(),
                    params=params,
                    timeout=30,
                )
            except (requests.ConnectionError, requests.Timeout) as error:
                if transient_retries >= len(GET_RETRY_DELAYS_SECONDS):
                    raise
                delay = GET_RETRY_DELAYS_SECONDS[transient_retries]
                transient_retries += 1
                self._show_retry(
                    f"{type(error).__name__}: {error}",
                    f"retrying request in {delay:g}s "
                    f"({transient_retries}/{len(GET_RETRY_DELAYS_SECONDS)})",
                )
                time.sleep(delay)
                continue

            if resp.status_code == 401 and not refreshed_auth:
                refreshed_auth = True
                self._show_retry(
                    "HTTP 401 Unauthorized",
                    "refreshing authentication and retrying request (1/1)",
                )
                self._authenticate()
                continue

            if (
                resp.status_code in RETRYABLE_HTTP_STATUSES
                and transient_retries < len(GET_RETRY_DELAYS_SECONDS)
            ):
                delay = GET_RETRY_DELAYS_SECONDS[transient_retries]
                transient_retries += 1
                self._show_retry(
                    f"HTTP {resp.status_code} {resp.reason}",
                    f"retrying request in {delay:g}s "
                    f"({transient_retries}/{len(GET_RETRY_DELAYS_SECONDS)})",
                )
                time.sleep(delay)
                continue

            resp.raise_for_status()
            return resp.json()

    def _show_retry(self, error: str, action: str) -> None:
        """Display automatic recovery so transient API failures are visible."""
        print(f"  ⚠ Questrade ({self.owner_name}): {error}; {action}")

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
        data = self._get("v1/markets/quotes", params={"ids": ids_str})
        return data.get("quotes", [])

    def get_symbols(self, symbol_ids: list) -> list:
        """
        Get symbol metadata for a list of Questrade symbol IDs.

        Args:
            symbol_ids: List of Questrade internal symbol IDs.

        Returns:
            List of symbol dictionaries including metadata such as currency.
        """
        if not symbol_ids:
            return []
        ids_str = ",".join(str(sid) for sid in symbol_ids)
        data = self._get("v1/symbols", params={"ids": ids_str})
        return data.get("symbols", [])

    def get_candles(
        self, symbol_id: int, start_time: str, end_time: str, interval: str = "OneDay"
    ) -> list:
        """
        Get historical OHLC candlestick data for a symbol.

        Args:
            symbol_id: Questrade internal symbol ID.
            start_time: ISO 8601 start datetime string.
            end_time: ISO 8601 end datetime string.
            interval: Candle interval (default "OneDay").

        Returns:
            List of candle dictionaries with open, high, low, close, volume.
        """
        data = self._get(
            f"v1/markets/candles/{symbol_id}",
            params={
                "startTime": start_time,
                "endTime": end_time,
                "interval": interval,
            },
        )
        return data.get("candles", [])

    def search_symbol(self, symbol: str) -> list:
        """
        Search for a symbol to get its Questrade symbol ID.

        Args:
            symbol: Ticker symbol (e.g., "ETF.TO").

        Returns:
            List of matching symbol dictionaries.
        """
        data = self._get("v1/symbols/search", params={"prefix": symbol})
        return data.get("symbols", [])
