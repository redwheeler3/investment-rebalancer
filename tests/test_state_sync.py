"""Test publication against a disposable bare remote. Ciphertext reads are checked in memory."""

import subprocess

import pytest

from src import encrypted_state, state_sync


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    root = tmp_path / "state"
    git(tmp_path, "init", "-b", "main", str(root))
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "fixture")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(tmp_path / "no-hooks"))
    (root / "tokens").mkdir()
    (root / "data").mkdir()
    (root / "tokens/a.json").write_bytes(b"cipher-initial")
    (root / "settings.yaml").write_bytes(b"initial")
    git(root, "add", ".")
    git(root, "commit", "-m", "baseline")
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-u", "origin", "main")
    monkeypatch.setenv("REBALANCER_STATE_DIR", str(root))

    def read(path, *, ciphertext=None):
        blob = path.read_bytes() if ciphertext is None else ciphertext
        if not blob.startswith(b"cipher-"):
            raise encrypted_state.EncryptedStateError("plaintext")
        return {}

    monkeypatch.setattr(encrypted_state, "read_encrypted_json", read)
    monkeypatch.setattr(state_sync, "read_encrypted_json", read)
    return root, remote


def test_rotation_retry_and_scoped_staging(repo, monkeypatch):
    root, remote = repo
    token = root / "tokens/a.json"
    token.write_bytes(b"cipher-rotated")
    (root / "settings.yaml").write_bytes(b"personal draft")
    git(root, "add", "settings.yaml")
    (root / "data/new.json").write_bytes(b"cipher-data")
    real_git = state_sync._git
    pushes = []

    def interrupted(root, *args):
        if args[0] == "push":
            pushes.append(args)
            if len(pushes) == 1:
                raise subprocess.CalledProcessError(1, "push")
        return real_git(root, *args)

    monkeypatch.setattr(state_sync, "_git", interrupted)
    state_sync.sync_state(token)
    assert len(pushes) == 2
    assert git(remote, "show", "main:tokens/a.json") == b"cipher-rotated"
    assert (
        git(remote, "diff-tree", "--no-commit-id", "-r", "--name-only", "main").strip()
        == b"tokens/a.json"
    )
    assert git(root, "diff", "--cached", "--name-only").strip() == b"settings.yaml"
    git(root, "add", "data/new.json")
    state_sync.sync_state()
    state_sync.sync_state()
    assert git(remote, "rev-list", "--count", "main").strip() == b"3"
    assert git(remote, "show", "main:settings.yaml") == b"initial"


@pytest.mark.parametrize("previous", ["cipher-ok", "plaintext", "unrelated"])
def test_retry_checks_all_unpublished_history(repo, previous):
    root, remote = repo
    path = "settings.yaml" if previous == "unrelated" else "tokens/a.json"
    (root / path).write_text(previous)
    git(root, "commit", "-am", "interrupted local commit")
    if previous == "cipher-ok":
        state_sync.sync_state()
        assert git(remote, "show", "main:tokens/a.json") == b"cipher-ok"
    else:
        (root / path).write_text("cipher-final")
        git(root, "commit", "-am", "later change")
        with pytest.raises(encrypted_state.EncryptedStateError):
            state_sync.sync_state()
        assert git(remote, "rev-list", "--count", "main").strip() == b"1"


@pytest.mark.parametrize("unexpected", ["export.csv", "nested/backup.json"])
def test_unexpected_file_stops_publication(repo, unexpected):
    root, remote = repo
    (root / "tokens/a.json").write_text("cipher-new")
    stray = root / "data" / unexpected
    stray.parent.mkdir(exist_ok=True)
    stray.write_text("plaintext")
    with pytest.raises(encrypted_state.EncryptedStateError, match="Unexpected"):
        state_sync.sync_state()
    assert git(remote, "show", "main:tokens/a.json") == b"cipher-initial"


def test_next_login_and_401_retry_see_published_token(repo, monkeypatch):
    from src import questrade_client as broker
    from unittest.mock import Mock

    root, remote = repo
    second = root / "tokens/b.json"
    second.write_bytes(b"cipher-initial-b")
    git(root, "add", "tokens/b.json")
    git(root, "commit", "-m", "second login")
    git(root, "push")
    rotations = []

    def exchange(url, data, timeout):
        if rotations:
            last, replacement = rotations[-1]
            assert git(remote, "show", f"main:tokens/{last}") == replacement.encode()
        old = data["refresh_token"]
        name = "b.json" if old.startswith("cipher-initial-b") else "a.json"
        new = f"cipher-rotated-{len(rotations)}"
        rotations.append((name, new))
        response = Mock()
        response.json.return_value = dict(
            refresh_token=new,
            access_token="demo",
            api_server="https://example.invalid/",
        )
        return response

    monkeypatch.setattr(broker.requests, "post", exchange)
    monkeypatch.setattr(
        broker, "read_encrypted_json", lambda p: {"refresh_token": p.read_text()}
    )
    monkeypatch.setattr(
        broker,
        "write_encrypted_json",
        lambda p, data: p.write_text(data["refresh_token"]),
    )
    client = broker.QuestradeClient(str(root / "tokens/a.json"), "A", {})
    broker.QuestradeClient(str(second), "B", {})
    replies = iter([401, 200])

    def get(*args, **kwargs):
        code = next(replies)
        if code == 200:
            assert git(remote, "show", "main:tokens/a.json") == b"cipher-rotated-2"
        response = Mock(status_code=code)
        response.json.return_value = {"ok": True}
        return response

    monkeypatch.setattr(broker.requests, "get", get)
    assert client._get("v1/example") == {"ok": True}
    assert len(rotations) == 3


def test_token_preflight_requires_remote_file_and_rotation_verifies_blob(
    repo, monkeypatch
):
    root, remote = repo
    ignored = root / "tokens/new.json"
    ignored.write_bytes(b"cipher-new")
    (root / ".git/info/exclude").write_text("tokens/new.json\n")
    # Real SOPS preflight is exercised by the encryption tests.
    token = root / "tokens/a.json"
    monkeypatch.setattr(
        state_sync, "validate_encrypted_state_files", lambda *args: [token, ignored]
    )
    with pytest.raises(state_sync.StateSyncError, match="Track and publish"):
        state_sync.prepare_state([ignored.name])
    state_sync.prepare_state([token.name])
    state_sync.sync_state(token)
    git(root, "update-index", "--assume-unchanged", "tokens/a.json")
    token.write_bytes(b"cipher-different")
    with pytest.raises(state_sync.StateSyncError, match="does not match"):
        state_sync.sync_state(token)
    assert git(remote, "show", "main:tokens/a.json") == b"cipher-initial"
