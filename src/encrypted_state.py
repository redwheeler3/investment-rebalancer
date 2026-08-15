"""SOPS-backed reads and atomic writes for sensitive private state."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from src.paths import get_state_root


SOPS_CONFIG_FILENAME = ".sops.yaml"
SOPS_EXECUTABLE_ENV = "SOPS_EXECUTABLE"
SOPS_TIMEOUT_SECONDS = 30


class EncryptedStateError(RuntimeError):
    """Raised when sensitive state cannot be decrypted or encrypted safely."""


def read_encrypted_json(path: Path) -> dict:
    """Decrypt and parse a SOPS-encrypted JSON object without plaintext files."""
    plaintext = _run_sops("decrypt", path, path.read_bytes(), "json")
    try:
        value = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EncryptedStateError(f"Decrypted state at '{path}' is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise EncryptedStateError(f"Decrypted state at '{path}' must be a JSON object.")
    return value


def write_encrypted_json(path: Path, value: dict) -> None:
    """Serialize and atomically persist a JSON object as SOPS ciphertext."""
    plaintext = json.dumps(value, indent=2).encode("utf-8") + b"\n"
    _write_encrypted(path, plaintext, "json")


def read_encrypted_text(path: Path) -> str:
    """Decrypt a SOPS binary document into UTF-8 text in memory."""
    plaintext = _run_sops("decrypt", path, path.read_bytes(), "binary")
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EncryptedStateError(f"Decrypted state at '{path}' is not UTF-8 text.") from exc


def write_encrypted_text(path: Path, value: str) -> None:
    """Atomically persist UTF-8 text as an opaque SOPS binary document."""
    _write_encrypted(path, value.encode("utf-8"), "binary")


def validate_encrypted_state_files() -> list[Path]:
    """Decrypt every managed state file in memory, failing on plaintext or corruption."""
    state_root = get_state_root()
    json_paths = sorted((state_root / "tokens").glob("*.json"))
    json_paths.extend(sorted((state_root / "data").glob("*.json")))
    text_paths = sorted((state_root / "data").glob("*.jsonl"))

    for path in json_paths:
        read_encrypted_json(path)
    for path in text_paths:
        read_encrypted_text(path)

    return json_paths + text_paths


def _write_encrypted(path: Path, plaintext: bytes, file_type: str) -> None:
    ciphertext = _run_sops("encrypt", path, plaintext, file_type)
    path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(ciphertext)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _run_sops(operation: str, path: Path, content: bytes, file_type: str) -> bytes:
    state_root = get_state_root()
    try:
        relative_path = path.resolve().relative_to(state_root).as_posix()
    except ValueError as exc:
        raise EncryptedStateError(
            f"Sensitive state path '{path}' must be inside '{state_root}'."
        ) from exc

    executable = _get_sops_executable()
    command = [executable]
    if operation == "encrypt":
        config_path = state_root / SOPS_CONFIG_FILENAME
        if not config_path.is_file():
            raise EncryptedStateError(
                f"Missing SOPS configuration at '{config_path}'."
            )
        command.extend(["--config", str(config_path)])

    command.extend([
        operation,
        "--filename-override",
        relative_path,
        "--input-type",
        file_type,
        "--output-type",
        file_type,
    ])

    try:
        result = subprocess.run(
            command,
            input=content,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=SOPS_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise EncryptedStateError(
            "SOPS is not installed or is not available on PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise EncryptedStateError(
            f"SOPS timed out while trying to {operation} '{relative_path}'."
        ) from exc

    if result.returncode != 0:
        raise EncryptedStateError(
            f"SOPS could not {operation} '{relative_path}' "
            f"(exit code {result.returncode})."
        )
    return result.stdout


def _get_sops_executable() -> str:
    configured = os.environ.get(SOPS_EXECUTABLE_ENV)
    if configured:
        return configured

    discovered = shutil.which("sops")
    if discovered:
        return discovered

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            installed = Path(local_app_data) / "Programs" / "SOPS" / "sops.exe"
            if installed.is_file():
                return str(installed)

    return "sops"


if __name__ == "__main__":
    validated = validate_encrypted_state_files()
    print(f"Validated {len(validated)} encrypted state file(s).")
