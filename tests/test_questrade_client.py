"""Tests for Questrade authentication and request recovery."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from src.questrade_client import QUESTRADE_AUTH_URL, QuestradeClient


def test_refreshes_with_post_body_and_persists_encrypted_replacement(tmp_path):
    token_path = tmp_path / "account.json"
    response = MagicMock()
    response.json.return_value = {
        "access_token": "access-token",
        "api_server": "https://api.example/v1/",
        "refresh_token": "replacement-token",
    }

    with (
        patch(
            "src.questrade_client.read_encrypted_json",
            return_value={"refresh_token": "current-token"},
        ),
        patch("src.questrade_client.write_encrypted_json") as write_encrypted,
        patch("src.questrade_client.requests.post", return_value=response) as post,
    ):
        client = QuestradeClient(str(token_path), "Alice", {})

    post.assert_called_once_with(
        QUESTRADE_AUTH_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": "current-token",
        },
        timeout=30,
    )
    response.raise_for_status.assert_called_once_with()
    write_encrypted.assert_called_once_with(
        token_path,
        {"refresh_token": "replacement-token"},
    )
    assert client.access_token == "access-token"
    assert client.refresh_token == "replacement-token"


def _client() -> QuestradeClient:
    client = QuestradeClient.__new__(QuestradeClient)
    client.owner_name = "Alice"
    client.api_server = "https://api.example/"
    client.access_token = "first-access-token"
    return client


def _response(status_code: int, reason: str, data: dict | None = None):
    response = requests.Response()
    response.status_code = status_code
    response.reason = reason
    response.url = "https://api.example/v1/test"
    response.json = MagicMock(return_value=data or {})
    return response


def test_get_refreshes_auth_once_after_401_and_displays_recovery(capsys):
    client = _client()
    unauthorized = _response(401, "Unauthorized")
    success = _response(200, "OK", {"value": 42})

    def refresh_auth():
        client.access_token = "replacement-access-token"
        client.api_server = "https://api02.example/"

    with (
        patch.object(client, "_authenticate", side_effect=refresh_auth) as authenticate,
        patch(
            "src.questrade_client.requests.get",
            side_effect=[unauthorized, success],
        ) as get,
    ):
        result = client._get("v1/test")

    assert result == {"value": 42}
    authenticate.assert_called_once_with()
    assert get.call_args_list[0].args[0] == "https://api.example/v1/test"
    assert get.call_args_list[1].args[0] == "https://api02.example/v1/test"
    output = capsys.readouterr().out
    assert "Questrade (Alice): HTTP 401 Unauthorized" in output
    assert "refreshing authentication and retrying request (1/1)" in output


def test_get_does_not_refresh_auth_twice_after_repeated_401():
    client = _client()

    with (
        patch.object(client, "_authenticate") as authenticate,
        patch(
            "src.questrade_client.requests.get",
            side_effect=[
                _response(401, "Unauthorized"),
                _response(401, "Unauthorized"),
            ],
        ),
        pytest.raises(requests.HTTPError),
    ):
        client._get("v1/test")

    authenticate.assert_called_once_with()


def test_get_retries_transient_http_errors_with_visible_backoff(capsys):
    client = _client()

    with (
        patch(
            "src.questrade_client.requests.get",
            side_effect=[
                _response(503, "Service Unavailable"),
                _response(429, "Too Many Requests"),
                _response(200, "OK", {"value": 42}),
            ],
        ) as get,
        patch("src.questrade_client.time.sleep") as sleep,
    ):
        result = client._get("v1/test")

    assert result == {"value": 42}
    assert get.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [0.5, 1.0]
    output = capsys.readouterr().out
    assert "HTTP 503 Service Unavailable; retrying request in 0.5s (1/2)" in output
    assert "HTTP 429 Too Many Requests; retrying request in 1s (2/2)" in output


def test_get_retries_connection_errors_then_raises(capsys):
    client = _client()
    error = requests.ConnectionError("connection interrupted")

    with (
        patch("src.questrade_client.requests.get", side_effect=error) as get,
        patch("src.questrade_client.time.sleep") as sleep,
        pytest.raises(requests.ConnectionError, match="connection interrupted"),
    ):
        client._get("v1/test")

    assert get.call_count == 3
    assert sleep.call_count == 2
    output = capsys.readouterr().out
    assert "ConnectionError: connection interrupted" in output
    assert "retrying request in 1s (2/2)" in output


def test_get_does_not_retry_other_client_errors():
    client = _client()

    with (
        patch(
            "src.questrade_client.requests.get",
            return_value=_response(403, "Forbidden"),
        ) as get,
        patch("src.questrade_client.time.sleep") as sleep,
        pytest.raises(requests.HTTPError),
    ):
        client._get("v1/test")

    get.assert_called_once_with(
        "https://api.example/v1/test",
        headers={"Authorization": "Bearer first-access-token"},
        params=None,
        timeout=30,
    )
    sleep.assert_not_called()
