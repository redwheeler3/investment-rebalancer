"""Tests for encrypted Questrade refresh-token rotation."""

from unittest.mock import MagicMock, patch

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
