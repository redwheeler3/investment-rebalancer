"""Publish only validated encrypted state to the private repository's main branch."""

import argparse
import subprocess
import time
from pathlib import Path

from src.encrypted_state import (
    EncryptedStateError,
    read_encrypted_json,
    read_encrypted_text,
    validate_encrypted_state_files,
)
from src.paths import get_state_root


class StateSyncError(EncryptedStateError):
    """The remote may lack the current token. Stop before another exchange."""


def _git(root, *args):
    # Git diagnostics or blob output can contain private state.
    result = subprocess.run(
        ["git", "--literal-pathspecs", *args],
        cwd=root,
        capture_output=True,
        timeout=60,
        check=True,
    )
    return result.stdout


def _paths(output):
    return [name.decode("utf-8") for name in output.split(b"\0") if name]


def _publish_pending(root, allowed, retry_seconds=0):
    branch = _git(root, "branch", "--show-current").strip()
    if branch != b"main":
        raise StateSyncError("Private state sync requires the main branch.")
    upstream = "refs/remotes/origin/main"
    local_tip = _git(root, "rev-parse", "HEAD").decode().strip()
    original_tip = _git(root, "rev-parse", upstream).decode().strip()
    ahead, behind = map(
        int,
        _git(
            root, "rev-list", "--left-right", "--count", f"{local_tip}...{original_tip}"
        ).split(),
    )
    if behind:
        raise StateSyncError(
            "Private state is behind origin/main. Reconcile before syncing."
        )
    if not ahead:
        return
    # Inspect every outgoing commit, including an earlier failed push. Checking
    # only the final tree could publish plaintext removed by a later commit.
    for commit in (
        _git(root, "rev-list", f"{original_tip}..{local_tip}").decode().split()
    ):
        if len(_git(root, "rev-list", "--parents", "-n1", commit).split()) != 2:
            raise StateSyncError("Automatic state sync cannot publish a merge.")
        names = _paths(
            _git(
                root,
                "diff-tree",
                "--no-commit-id",
                "-r",
                "--no-renames",
                "--name-only",
                "-z",
                commit,
            )
        )
        if not names or not set(names) <= allowed:
            raise StateSyncError(
                "Outgoing commits include changes outside managed state."
            )
        for name in names:
            entry = _git(root, "ls-tree", "-z", commit, "--", name)
            if not entry.startswith(b"100644 blob "):
                raise StateSyncError(
                    "Unexpected deletion or file mode in outgoing state."
                )
            blob = _git(root, "cat-file", "blob", f"{commit}:{name}")
            path = root / name
            reader = (
                read_encrypted_text if path.suffix == ".jsonl" else read_encrypted_json
            )
            reader(path, ciphertext=blob)

    deadline = time.monotonic() + retry_seconds
    delay = 5
    first = True
    while True:
        try:
            _git(root, "push", "origin", f"{local_tip}:refs/heads/main")
            if _git(root, "rev-parse", upstream).decode().strip() != local_tip:
                raise StateSyncError(
                    "Remote state synchronization could not be verified."
                )
            return
        except (subprocess.SubprocessError, OSError):
            # A lost reply can follow a successful push. A different remote tip
            # needs reconciliation; never force a single-use token over it.
            try:
                refs = (
                    _git(root, "ls-remote", "origin", "refs/heads/main")
                    .decode()
                    .split()
                )
            except (subprocess.SubprocessError, OSError):
                refs = []
            if refs and refs[0] == local_tip:
                _git(root, "fetch", "origin", "main")
                return
            if refs and refs[0] != original_tip:
                raise StateSyncError(
                    "Remote main advanced. Preserve and reconcile the local state."
                )
            remaining = deadline - time.monotonic()
            if not first and remaining <= 0:
                raise
            print("Private state push failed. Retrying the saved commit.", flush=True)
            if remaining > 0:
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, 300)
            first = False


def prepare_state(token_names):
    """Verify encryption and recover prior commits before consuming any token."""
    root = get_state_root()
    paths = validate_encrypted_state_files(token_names)
    try:
        if (
            Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve()
            != root
        ):
            raise StateSyncError("State directory must be the private repository root.")
        _git(root, "var", "GIT_AUTHOR_IDENT")
        _git(root, "var", "GIT_COMMITTER_IDENT")
        _publish_pending(root, {p.relative_to(root).as_posix() for p in paths})
    except (subprocess.SubprocessError, OSError) as exc:
        raise StateSyncError(
            "Cannot publish private state. Check branch, remote and Git identity."
        ) from exc


def sync_state(token: Path | None = None, *, retry_seconds=0):
    """Commit a single rotated token immediately, or all managed files at run end."""
    root = get_state_root()
    try:
        allowed = {
            p.relative_to(root).as_posix() for p in validate_encrypted_state_files()
        }
        _publish_pending(root, allowed, retry_seconds)
        selected = (
            {token.relative_to(root).as_posix()} if token is not None else allowed
        )
        if not selected <= allowed:
            raise StateSyncError("Token is outside the validated state files.")
        if not selected:
            return
        paths = sorted(selected)
        if not _git(
            root, "status", "--porcelain", "--untracked-files=all", "--", *paths
        ):
            return
        _git(root, "add", "--", *paths)
        if _git(root, "diff", "--cached", "--name-only", "-z", "--", *paths):
            _git(
                root,
                "commit",
                "--only",
                "-m",
                "chore(sync): persist encrypted state",
                "--",
                *paths,
            )
        _publish_pending(root, allowed, retry_seconds)
    except (subprocess.SubprocessError, OSError) as error:
        raise StateSyncError(
            "Private state sync failed. Preserve the local state and verify origin/main "
            "before refreshing another token."
        ) from error


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retry-seconds", type=int, default=0, help="Recovery window for a failed push"
    )
    sync_state(retry_seconds=parser.parse_args().retry_seconds)
