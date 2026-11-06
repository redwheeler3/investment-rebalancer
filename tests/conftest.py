"""Shared in-memory encrypted-state doubles; never use live private state."""

from copy import deepcopy

import pytest

from src import external_accounts, transfer_tracking


@pytest.fixture
def encrypted_store(tmp_path, monkeypatch):
    """Keep test plaintext in memory while paths contain only ciphertext markers."""
    data = tmp_path / "data"
    data.mkdir()
    storage = {}

    def read(path):
        return deepcopy(storage[path])

    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("encrypted-test-placeholder", encoding="utf-8")
        storage[path] = deepcopy(value)

    for module in (transfer_tracking, external_accounts):
        monkeypatch.setattr(module, "read_encrypted_json", read)
        monkeypatch.setattr(module, "write_encrypted_json", write)
        monkeypatch.setattr(module, "get_data_dir", lambda: data)
    return data, storage
