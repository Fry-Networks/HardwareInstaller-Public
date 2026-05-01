"""
Miner registry loader with CDN fetch, disk cache, and bundled fallback.

Provides two entry points:

- ``load_local_registry()``  — import-time loader; reads disk cache then
  bundled JSON.  **No network I/O.**  Never raises.
- ``refresh_from_cdn()``     — foreground CDN fetch with SHA-256 envelope
  verification.  Writes result to disk cache on success.  Returns the
  registry dict or ``None`` on any failure.

Fallback chain (both entry points combined):
  CDN (3 s timeout) → disk cache → bundled JSON.

Cache location: ``C:\\ProgramData\\FryNetworks\\cache\\miner_registry.json``
Atomic write: ``.json.tmp`` + ``os.replace`` (matches tools/updater.py:452-454).
"""

from __future__ import annotations

import hashlib
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

_CDN_REGISTRY_URL = (
    "https://frynetworks-downloads.b-cdn.net/frynetworks-installer/manifest/v1/registry.json"
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


def _compute_sha256_str(data: str) -> str:
    """Return the hex SHA-256 digest of *data* (UTF-8 encoded)."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _fetch_envelope(url: str, timeout: int) -> Optional[Dict[str, Any]]:
    """Fetch the integrity envelope from *url*.  Returns parsed dict or None."""
    try:
        import urllib.request  # deferred: keep import-time path network-free
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw)
    except Exception as exc:
        _logger.debug("CDN fetch failed (%s): %s", url, exc)
        return None


def _verify_envelope(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Verify the SHA-256 integrity of *envelope*.

    Returns the inner ``registry`` dict on success, or None on failure.
    """
    expected_sha = envelope.get("sha256")
    registry = envelope.get("registry")

    if not expected_sha or not isinstance(registry, dict):
        _logger.debug("Envelope missing sha256 or registry field")
        return None

    canonical = json.dumps(registry, sort_keys=True, separators=(",", ":"))
    actual_sha = _compute_sha256_str(canonical)

    if expected_sha.lower() != actual_sha.lower():
        _logger.warning(
            "SHA-256 mismatch: expected %s, got %s",
            expected_sha[:16] + "...",
            actual_sha[:16] + "...",
        )
        return None

    if not _validate_schema_version(registry):
        _logger.warning(
            "CDN registry has unsupported schema_version: %s",
            registry.get("schema_version"),
        )
        return None

    if "miners" not in registry:
        _logger.debug("CDN registry missing 'miners' key")
        return None

    return registry


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


def refresh_from_cdn(
    url: Optional[str] = None,
    timeout: int = 3,
) -> Optional[Dict[str, Any]]:
    """Fetch registry from CDN, verify SHA-256, and update disk cache.

    Returns the registry dict on success, or ``None`` on any failure.
    Worst-case latency: *timeout* seconds (broken DNS / partial connectivity).
    Confirmed-offline (adapter disabled) typically returns immediately.
    """
    target = url or _CDN_REGISTRY_URL

    envelope = _fetch_envelope(target, timeout)
    if envelope is None:
        return None

    registry = _verify_envelope(envelope)
    if registry is None:
        return None

    _write_cache(registry)
    return registry
