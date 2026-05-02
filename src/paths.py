"""
Path helpers for the public code repo / private state repo split.

All mutable and personal state lives outside the public repository and is
located through the REBALANCER_STATE_DIR environment variable.
"""

from __future__ import annotations

import os
from pathlib import Path


ENV_VAR_NAME = "REBALANCER_STATE_DIR"
CODE_ROOT = Path(__file__).resolve().parent.parent


def get_state_root() -> Path:
    """Return the private state repo root from REBALANCER_STATE_DIR.

    Raises:
        RuntimeError: If the environment variable is missing or invalid.
    """
    raw = os.environ.get(ENV_VAR_NAME)
    if not raw:
        raise RuntimeError(
            f"{ENV_VAR_NAME} is not set. Point it at your private state repo "
            "that contains config/, tokens/, and data/. See README.md for setup instructions."
        )

    state_root = Path(raw).expanduser().resolve()
    if not state_root.exists() or not state_root.is_dir():
        raise RuntimeError(
            f"{ENV_VAR_NAME} points to '{state_root}', but that directory does not exist."
        )

    return state_root


def _required_subdir(name: str) -> Path:
    """Return a required subdirectory inside the private state repo."""
    path = get_state_root() / name
    if not path.exists() or not path.is_dir():
        raise RuntimeError(
            f"Private state repo is missing required directory '{name}/' at '{path}'."
        )
    return path


def get_tokens_dir() -> Path:
    """Return the tokens directory inside the private state repo."""
    return _required_subdir("tokens")


def get_config_dir() -> Path:
    """Return the config directory inside the private state repo."""
    return _required_subdir("config")


def get_data_dir() -> Path:
    """Return the data directory inside the private state repo.

    The directory is created lazily because first-time users may not yet have
    portfolio history recorded.
    """
    return get_state_root() / "data"


def get_history_file() -> Path:
    """Return the canonical portfolio history file inside the private state repo."""
    return get_data_dir() / "portfolio_history.jsonl"


def get_legacy_history_file() -> Path:
    """Return the legacy portfolio history filename for backward compatibility."""
    return get_data_dir() / "portfolio_history.json"
