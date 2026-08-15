"""Tests for in-memory SOPS encryption and atomic ciphertext persistence."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.encrypted_state import (
    EncryptedStateError,
    read_encrypted_json,
    read_encrypted_text,
    validate_encrypted_state_files,
    write_encrypted_json,
    write_encrypted_text,
)


@pytest.fixture
def state_root(tmp_path):
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n", encoding="utf-8")
    with patch("src.encrypted_state.get_state_root", return_value=tmp_path):
        yield tmp_path


def _completed(stdout: bytes, returncode: int = 0, stderr: bytes = b""):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


def test_decrypts_json_through_stdin(state_root):
    token_path = state_root / "tokens" / "account.json"
    token_path.parent.mkdir()
    token_path.write_bytes(b"encrypted-document")

    with patch(
        "src.encrypted_state.subprocess.run",
        return_value=_completed(b'{"refresh_token":"decrypted-token"}\n'),
    ) as run:
        result = read_encrypted_json(token_path)

    assert result == {"refresh_token": "decrypted-token"}
    command = run.call_args.args[0]
    assert command[1] == "decrypt"
    assert command[command.index("--filename-override") + 1] == "tokens/account.json"
    assert run.call_args.kwargs["input"] == b"encrypted-document"
    assert "decrypted-token" not in " ".join(command)


def test_encrypts_json_and_atomically_replaces_ciphertext(state_root):
    token_path = state_root / "tokens" / "account.json"
    token_path.parent.mkdir()
    token_path.write_bytes(b"old-ciphertext")

    with patch(
        "src.encrypted_state.subprocess.run",
        return_value=_completed(b"new-ciphertext"),
    ) as run:
        write_encrypted_json(token_path, {"refresh_token": "rotated-token"})

    assert token_path.read_bytes() == b"new-ciphertext"
    assert not list(token_path.parent.glob(".account.json.*.tmp"))
    command = run.call_args.args[0]
    assert "encrypt" in command
    assert "--config" in command
    assert json.loads(run.call_args.kwargs["input"])["refresh_token"] == "rotated-token"
    assert "rotated-token" not in " ".join(command)


def test_failed_encryption_preserves_existing_ciphertext(state_root):
    token_path = state_root / "tokens" / "account.json"
    token_path.parent.mkdir()
    token_path.write_bytes(b"current-ciphertext")

    with (
        patch(
            "src.encrypted_state.subprocess.run",
            return_value=_completed(b"", returncode=1, stderr=b"sensitive diagnostic"),
        ),
        pytest.raises(EncryptedStateError) as exc_info,
    ):
        write_encrypted_json(token_path, {"refresh_token": "rotated-token"})

    assert token_path.read_bytes() == b"current-ciphertext"
    assert "rotated-token" not in str(exc_info.value)
    assert "sensitive diagnostic" not in str(exc_info.value)


def test_encrypts_history_as_binary_without_plaintext_file(state_root):
    history_path = state_root / "data" / "portfolio_history.jsonl"

    with patch(
        "src.encrypted_state.subprocess.run",
        return_value=_completed(b"encrypted-history"),
    ) as run:
        write_encrypted_text(history_path, '{"date":"2026-08-14","value":1}\n')

    assert history_path.read_bytes() == b"encrypted-history"
    command = run.call_args.args[0]
    assert command[command.index("--input-type") + 1] == "binary"
    assert command[command.index("--output-type") + 1] == "binary"


def test_rejects_sensitive_path_outside_state_root(state_root, tmp_path):
    outside_path = tmp_path.parent / "outside.json"
    outside_path.write_bytes(b"encrypted")

    with pytest.raises(EncryptedStateError, match="must be inside"):
        read_encrypted_json(outside_path)


def test_requires_sops_config_before_encrypting(state_root):
    (state_root / ".sops.yaml").unlink()
    token_path = state_root / "tokens" / "account.json"

    with pytest.raises(EncryptedStateError, match="Missing SOPS configuration"):
        write_encrypted_json(token_path, {"refresh_token": "token"})


def test_validates_every_managed_state_file(state_root):
    token_path = state_root / "tokens" / "account.json"
    tactical_path = state_root / "data" / "tactical_state.json"
    fx_path = state_root / "data" / "fx_targets_state.json"
    history_path = state_root / "data" / "portfolio_history.jsonl"
    for path in (token_path, tactical_path, fx_path, history_path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ciphertext")

    with (
        patch("src.encrypted_state.read_encrypted_json") as read_json,
        patch("src.encrypted_state.read_encrypted_text") as read_text,
    ):
        validated = validate_encrypted_state_files()

    assert validated == [token_path, fx_path, tactical_path, history_path]
    assert [call.args[0] for call in read_json.call_args_list] == [
        token_path,
        fx_path,
        tactical_path,
    ]
    read_text.assert_called_once_with(history_path)


@pytest.mark.skipif(
    os.environ.get("RUN_SOPS_INTEGRATION") != "1",
    reason="requires an installed SOPS binary and disposable age key",
)
def test_real_sops_round_trip_keeps_plaintext_off_disk(tmp_path, monkeypatch):
    sops_executable = os.environ["SOPS_EXECUTABLE"]
    age_key_file = Path(os.environ["SOPS_AGE_KEY_FILE"])
    age_keygen = os.environ["AGE_KEYGEN_EXECUTABLE"]
    recipient = subprocess.run(
        [age_keygen, "-y", str(age_key_file)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (tmp_path / ".sops.yaml").write_text(
        "creation_rules:\n"
        "  - path_regex: '(tokens.*\\.json|data.*\\.(json|jsonl))$'\n"
        f"    age: {recipient}\n",
        encoding="utf-8",
    )
    real_run = subprocess.run

    def run_sops_with_test_diagnostics(*args, **kwargs):
        result = real_run(*args, **kwargs)
        if result.returncode != 0:
            pytest.fail(result.stderr.decode("utf-8", errors="replace"))
        return result

    monkeypatch.setattr("src.encrypted_state.get_state_root", lambda: tmp_path)
    monkeypatch.setattr(
        "src.encrypted_state.subprocess.run", run_sops_with_test_diagnostics
    )
    monkeypatch.setenv("SOPS_EXECUTABLE", sops_executable)

    token_path = tmp_path / "tokens" / "account.json"
    history_path = tmp_path / "data" / "portfolio_history.jsonl"
    tactical_path = tmp_path / "data" / "tactical_state.json"
    fx_path = tmp_path / "data" / "fx_targets_state.json"
    write_encrypted_json(token_path, {"refresh_token": "round-trip-token"})
    write_encrypted_text(history_path, '{"date":"2026-08-14","value":1}\n')
    write_encrypted_json(tactical_path, {"regime": "baseline"})
    write_encrypted_json(fx_path, {"split": {"cad_target_pct": 56.0}})

    assert b"round-trip-token" not in token_path.read_bytes()
    assert b'"value":1' not in history_path.read_bytes()
    assert b'"baseline"' not in tactical_path.read_bytes()
    assert b"56.0" not in fx_path.read_bytes()
    assert read_encrypted_json(token_path) == {"refresh_token": "round-trip-token"}
    assert read_encrypted_text(history_path) == '{"date":"2026-08-14","value":1}\n'
    assert read_encrypted_json(tactical_path) == {"regime": "baseline"}
    assert read_encrypted_json(fx_path) == {"split": {"cad_target_pct": 56.0}}
    assert validate_encrypted_state_files() == [
        token_path,
        fx_path,
        tactical_path,
        history_path,
    ]
    assert not list(tmp_path.rglob("*.tmp"))
