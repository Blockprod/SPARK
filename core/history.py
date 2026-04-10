"""Canonical persistence helpers for shorts-engine.

Centralises:
- Reading / appending ``logs/publish_history.jsonl``
- Loading ``config.yaml`` and ``.env`` (single source of truth shared by
  pipeline.py, dashboard/app.py, and any future entrypoint)

All functions are synchronous and intentionally thin so they can be called
from both sync and async contexts without an event-loop dependency.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)

_HISTORY_FILENAME = "publish_history.jsonl"


class HistoryError(RuntimeError):
    """Raised when a history read / write operation fails unrecoverably."""


# ---------------------------------------------------------------------------
# publish_history.jsonl helpers
# ---------------------------------------------------------------------------


def read_publish_history(logs_dir: Path) -> list[dict[str, Any]]:
    """Read and parse every entry in ``logs/publish_history.jsonl``.

    Malformed lines (empty or invalid JSON) are silently skipped.

    Args:
        logs_dir: Directory that contains ``publish_history.jsonl``.

    Returns:
        List of entry dicts in file order.  Empty list if the file is absent.
    """
    history_path = logs_dir / _HISTORY_FILENAME
    if not history_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    try:
        with history_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        LOGGER.warning("Could not read %s: %s", history_path, exc)
    return entries


def append_publish_history(logs_dir: Path, entry: dict[str, Any]) -> None:
    """Append a single JSON entry to ``logs/publish_history.jsonl``.

    Creates the file (and parent directory) if absent.

    Args:
        logs_dir: Directory that contains (or will contain) the JSONL file.
        entry: Dictionary to serialise as a single JSON line.

    Raises:
        HistoryError: If the write fails.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    history_path = logs_dir / _HISTORY_FILENAME
    try:
        with history_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        raise HistoryError(
            f"Could not append to {history_path}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Config / env loaders (canonical — single source of truth)
# ---------------------------------------------------------------------------


def load_config(config_path: Path | str = "config.yaml") -> dict[str, Any]:
    """Load and return the parsed ``config.yaml`` mapping.

    Args:
        config_path: Relative or absolute path to ``config.yaml``.

    Returns:
        Parsed configuration dictionary.

    Raises:
        HistoryError: If the file is missing or unparseable.
    """
    path = Path(config_path).resolve()
    if not path.exists():
        raise HistoryError(f"Configuration file not found: {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
    except Exception as exc:
        raise HistoryError(f"Failed to parse config.yaml: {exc}") from exc
    if not isinstance(cfg, dict):
        raise HistoryError("config.yaml must be a YAML mapping at the top level.")
    return cfg


def load_env(env_path: Path | str | None = ".env") -> dict[str, str]:
    """Load a ``.env`` file into the process environment and return a snapshot.

    If *env_path* is ``None`` or the file does not exist, the function returns
    the current environment without modification.

    Args:
        env_path: Path to the ``.env`` file.  Pass ``None`` to skip loading.

    Returns:
        Dictionary snapshot of ``os.environ`` after optional dotenv loading.
    """
    if env_path is not None:
        path = Path(env_path)
        if path.exists():
            load_dotenv(dotenv_path=str(path), override=False)
    return dict(os.environ)
