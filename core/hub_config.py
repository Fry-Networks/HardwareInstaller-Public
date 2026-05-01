"""Shared hub_config.json read/write for CLI and GUI."""
import json
import os
from pathlib import Path
import logging

_logger = logging.getLogger(__name__)


def hub_config_path() -> Path:
    return (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        / "FryNetworks"
        / "hub_config.json"
    )


def read_hub_config() -> dict:
    defaults = {
        "auto_update_hub": False,
        "last_update_check_at": None,
        "last_seen_hub_version": None,
    }
    try:
        text = hub_config_path().read_text(encoding="utf-8")
        data = json.loads(text)
        if isinstance(data, dict):
            defaults.update(data)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError, OSError):
        pass
    return defaults


def write_hub_config(config: dict) -> None:
    path = hub_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(path))
    except (PermissionError, OSError) as exc:
        _logger.warning("Could not write hub_config.json: %s", exc)
