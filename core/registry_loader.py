"""
Miner registry loader with remote fetch, disk cache, and bundled fallback.

Provides two entry points:

- ``load_local_registry()``  — import-time loader; reads disk cache then
  bundled JSON.  **No network I/O.**  Never raises.
- ``refresh_from_remote()``  — foreground fetch from GitHub with
  schema_version validation.  Writes result to disk cache on success.
  Returns the registry dict or ``None`` on any failure.

Fallback chain (both entry points combined):
  GitHub (3 s timeout) → disk cache → bundled JSON.

Cache location: ``C:\\ProgramData\\FryNetworks\\cache\\miner_registry.json``
Atomic write: ``.json.tmp`` + ``os.replace`` (matches tools/updater.py:452-454).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REGISTRY_URL = (
    "https://raw.githubusercontent.com/Fry-Foundation/"
    "HardwareInstaller-Public/main/core/miner_registry.json"
)

_CACHE_DIR = Path(r"C:\ProgramData\FryNetworks\cache")
_CACHE_FILE = _CACHE_DIR / "miner_registry.json"

_BUNDLED_PATH = Path(__file__).parent / "miner_registry.json"

_SUPPORTED_SCHEMA_VERSIONS = {1}

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_schema_version(registry: Dict[str, Any]) -> bool:
    """Return True if *registry* has a supported schema_version."""
    ver = registry.get("schema_version")
    return ver in _SUPPORTED_SCHEMA_VERSIONS


def _read_bundled() -> Dict[str, Any]:
    """Read the bundled registry shipped inside the package / PyInstaller exe."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
        path = base / "core" / "miner_registry.json"
    else:
        path = _BUNDLED_PATH
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_cache() -> Optional[Dict[str, Any]]:
    """Read cached registry from disk.  Returns None on any failure."""
    try:
        if not _CACHE_FILE.is_file():
            return None
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not _validate_schema_version(data):
            _logger.debug("Cache has unsupported schema_version: %s", data.get("schema_version"))
            return None
        if "miners" not in data:
            _logger.debug("Cache missing 'miners' key")
            return None
        return data
    except Exception as exc:
        _logger.debug("Cache read failed: %s", exc)
        return None


def _write_cache(registry: Dict[str, Any]) -> None:
    """Atomically write *registry* to the disk cache.

    Creates the cache directory if it does not exist.  Catches
    ``PermissionError`` so non-elevated runs do not crash.
    """
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(_CACHE_FILE))
    except PermissionError:
        _logger.warning("Cache write denied (non-elevated): %s", _CACHE_FILE)
    except Exception as exc:
        _logger.warning("Cache write failed: %s", exc)


def _fetch_registry(url: str, timeout: int) -> Optional[Dict[str, Any]]:
    """Fetch registry JSON from *url*.  Returns parsed dict or None."""
    try:
        import urllib.request  # deferred: keep import-time path network-free
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        _logger.debug("Registry fetch failed (%s): %s", url, exc)
        return None

    if not isinstance(data, dict):
        return None

    if not _validate_schema_version(data):
        _logger.warning(
            "Remote registry has unsupported schema_version: %s",
            data.get("schema_version"),
        )
        return None

    if "miners" not in data:
        _logger.debug("Remote registry missing 'miners' key")
        return None

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_local_registry() -> Dict[str, Any]:
    """Load the miner registry from local sources only (no network).

    Fallback order: disk cache → bundled JSON.
    Called at module-import time by ``core.key_parser``.  Never raises.
    """
    # 1. Try disk cache
    cached = _read_cache()
    if cached is not None:
        return cached

    # 2. Bundled fallback (always available)
    try:
        bundled = _read_bundled()
        return bundled
    except Exception as exc:
        _logger.error("Failed to read bundled registry: %s", exc)
        # Absolute last resort: return minimal valid structure
        return {"schema_version": 1, "miners": []}


def refresh_from_remote(
    url: Optional[str] = None,
    timeout: int = 3,
) -> Optional[Dict[str, Any]]:
    """Fetch registry from GitHub, validate schema, and update disk cache.

    Returns the registry dict on success, or ``None`` on any failure.
    Worst-case latency: *timeout* seconds (broken DNS / partial connectivity).
    Confirmed-offline (adapter disabled) typically returns immediately.
    """
    target = url or _REGISTRY_URL

    registry = _fetch_registry(target, timeout)
    if registry is None:
        return None

    _write_cache(registry)
    return registry


# Backward-compat alias (callers may still reference old name)
refresh_from_cdn = refresh_from_remote
