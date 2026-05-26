"""Unit tests for core.registry_loader — CDN fetch, cache, and fallback chain.

Uses stdlib unittest only (no pytest dependency).
Run: python -m unittest tests.test_registry_loader -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import URLError

# Ensure repo root on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import registry_loader


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_REGISTRY = {
    "schema_version": 1,
    "miners": [
        {
            "code": "BM",
            "name": "Bandwidth Miner",
            "group": "BM",
            "exclusive": None,
            "requires_installer": True,
            "requires_stake": False,
        },
        {
            "code": "IDM",
            "name": "Indoor Decibel Miner",
            "group": "Decibel",
            "exclusive": "ODM",
            "requires_installer": True,
            "requires_stake": False,
        },
    ],
}


def _canonical(registry: dict) -> str:
    return json.dumps(registry, sort_keys=True, separators=(",", ":"))


def _make_envelope(registry: dict | None = None, *, bad_sha: bool = False) -> dict:
    reg = registry or _SAMPLE_REGISTRY
    canonical = _canonical(reg)
    sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if bad_sha:
        sha = "0" * 64
    return {"manifest_version": "1.0.0", "sha256": sha, "registry": reg}


def _envelope_bytes(envelope: dict) -> bytes:
    return json.dumps(envelope).encode("utf-8")


class _FakeResponse:
    """Minimal file-like returned by urlopen mock."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


# ---------------------------------------------------------------------------
# CDN fetch tests
# ---------------------------------------------------------------------------


class TestRefreshFromCdn(unittest.TestCase):
    """Tests for refresh_from_cdn()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regtest_")
        self._orig_cache_dir = registry_loader._CACHE_DIR
        self._orig_cache_file = registry_loader._CACHE_FILE
        registry_loader._CACHE_DIR = Path(self.tmp)
        registry_loader._CACHE_FILE = Path(self.tmp) / "miner_registry.json"

    def tearDown(self):
        registry_loader._CACHE_DIR = self._orig_cache_dir
        registry_loader._CACHE_FILE = self._orig_cache_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    @mock.patch("urllib.request.urlopen")
    def test_cdn_fetch_success(self, mock_urlopen):
        envelope = _make_envelope()
        mock_urlopen.return_value = _FakeResponse(_envelope_bytes(envelope))

        result = registry_loader.refresh_from_cdn(timeout=1)

        self.assertIsNotNone(result)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(len(result["miners"]), 2)
        # Cache file should have been written
        self.assertTrue(registry_loader._CACHE_FILE.is_file())
        cached = json.loads(registry_loader._CACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(cached["schema_version"], 1)

    @mock.patch("urllib.request.urlopen")
    def test_cdn_fetch_timeout(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("timed out")

        result = registry_loader.refresh_from_cdn(timeout=1)

        self.assertIsNone(result)

    @mock.patch("urllib.request.urlopen")
    def test_cdn_fetch_bad_sha256(self, mock_urlopen):
        envelope = _make_envelope(bad_sha=True)
        mock_urlopen.return_value = _FakeResponse(_envelope_bytes(envelope))

        result = registry_loader.refresh_from_cdn(timeout=1)

        self.assertIsNone(result)
        # Cache must NOT be written on sha256 mismatch
        self.assertFalse(registry_loader._CACHE_FILE.is_file())

    @mock.patch("urllib.request.urlopen")
    def test_cdn_fetch_bad_json(self, mock_urlopen):
        mock_urlopen.return_value = _FakeResponse(b"not json {{{")

        result = registry_loader.refresh_from_cdn(timeout=1)

        self.assertIsNone(result)

    @mock.patch("urllib.request.urlopen")
    def test_cdn_fetch_wrong_schema(self, mock_urlopen):
        bad_reg = {**_SAMPLE_REGISTRY, "schema_version": 99}
        envelope = _make_envelope(bad_reg)
        mock_urlopen.return_value = _FakeResponse(_envelope_bytes(envelope))

        result = registry_loader.refresh_from_cdn(timeout=1)

        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Local load tests
# ---------------------------------------------------------------------------


class TestLoadLocalRegistry(unittest.TestCase):
    """Tests for load_local_registry()."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regtest_")
        self._orig_cache_dir = registry_loader._CACHE_DIR
        self._orig_cache_file = registry_loader._CACHE_FILE
        registry_loader._CACHE_DIR = Path(self.tmp)
        registry_loader._CACHE_FILE = Path(self.tmp) / "miner_registry.json"

    def tearDown(self):
        registry_loader._CACHE_DIR = self._orig_cache_dir
        registry_loader._CACHE_FILE = self._orig_cache_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_local_cache_hit(self):
        # Write a valid cache file
        registry_loader._CACHE_FILE.write_text(
            json.dumps(_SAMPLE_REGISTRY, indent=2) + "\n",
            encoding="utf-8",
        )

        result = registry_loader.load_local_registry()

        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(len(result["miners"]), 2)

    def test_load_local_cache_corrupt(self):
        # Write corrupt JSON to cache
        registry_loader._CACHE_FILE.write_text("{{not json", encoding="utf-8")

        result = registry_loader.load_local_registry()

        # Should fall back to bundled — bundled has 10 miners
        self.assertIn("miners", result)
        self.assertGreater(len(result["miners"]), 0)

    def test_load_local_cache_miss(self):
        # No cache file exists
        result = registry_loader.load_local_registry()

        # Falls back to bundled
        self.assertIn("miners", result)
        self.assertGreater(len(result["miners"]), 0)

    def test_load_local_bundled(self):
        """Bundled JSON loads successfully."""
        result = registry_loader._read_bundled()

        self.assertEqual(result["schema_version"], 1)
        self.assertIn("miners", result)
        # Bundled has 10 miner types as of Phase 1
        self.assertEqual(len(result["miners"]), 10)


# ---------------------------------------------------------------------------
# Fallback chain test
# ---------------------------------------------------------------------------


class TestFallbackChain(unittest.TestCase):
    """Test the full CDN → cache → bundled fallback chain."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regtest_")
        self._orig_cache_dir = registry_loader._CACHE_DIR
        self._orig_cache_file = registry_loader._CACHE_FILE
        registry_loader._CACHE_DIR = Path(self.tmp)
        registry_loader._CACHE_FILE = Path(self.tmp) / "miner_registry.json"

    def tearDown(self):
        registry_loader._CACHE_DIR = self._orig_cache_dir
        registry_loader._CACHE_FILE = self._orig_cache_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    @mock.patch("urllib.request.urlopen")
    def test_fallback_chain_full(self, mock_urlopen):
        """CDN fails, no cache → bundled used."""
        mock_urlopen.side_effect = URLError("no network")

        # refresh_from_cdn returns None
        cdn_result = registry_loader.refresh_from_cdn(timeout=1)
        self.assertIsNone(cdn_result)

        # load_local_registry falls back to bundled (no cache)
        local_result = registry_loader.load_local_registry()
        self.assertIn("miners", local_result)
        self.assertEqual(local_result["schema_version"], 1)


# ---------------------------------------------------------------------------
# Cache write tests
# ---------------------------------------------------------------------------


class TestCacheWrite(unittest.TestCase):
    """Tests for _write_cache behaviour."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="regtest_")
        self._orig_cache_dir = registry_loader._CACHE_DIR
        self._orig_cache_file = registry_loader._CACHE_FILE
        registry_loader._CACHE_DIR = Path(self.tmp)
        registry_loader._CACHE_FILE = Path(self.tmp) / "miner_registry.json"

    def tearDown(self):
        registry_loader._CACHE_DIR = self._orig_cache_dir
        registry_loader._CACHE_FILE = self._orig_cache_file
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_atomic_write(self):
        """Cache write uses .tmp + os.replace pattern."""
        registry_loader._write_cache(_SAMPLE_REGISTRY)

        self.assertTrue(registry_loader._CACHE_FILE.is_file())
        # .tmp file should NOT remain
        tmp = registry_loader._CACHE_FILE.with_suffix(".json.tmp")
        self.assertFalse(tmp.exists())
        # Content round-trips correctly
        loaded = json.loads(registry_loader._CACHE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(loaded["schema_version"], 1)

    def test_cache_creates_directory(self):
        """Cache write creates missing parent directories."""
        nested = Path(self.tmp) / "deep" / "nested"
        registry_loader._CACHE_DIR = nested
        registry_loader._CACHE_FILE = nested / "miner_registry.json"

        registry_loader._write_cache(_SAMPLE_REGISTRY)

        self.assertTrue(registry_loader._CACHE_FILE.is_file())

    @mock.patch("core.registry_loader._CACHE_DIR")
    def test_cache_permission_denied(self, mock_dir):
        """PermissionError on cache write does not crash."""
        mock_dir.mkdir.side_effect = PermissionError("access denied")

        # Should not raise
        registry_loader._write_cache(_SAMPLE_REGISTRY)


# ---------------------------------------------------------------------------
# Frozen mode path test
# ---------------------------------------------------------------------------


class TestFrozenMode(unittest.TestCase):
    """Test bundled path resolution in PyInstaller frozen mode."""

    def test_frozen_mode_path(self):
        """When sys.frozen is True, _read_bundled reads from _MEIPASS/core/."""
        fake_meipass = tempfile.mkdtemp(prefix="meipass_")
        core_dir = Path(fake_meipass) / "core"
        core_dir.mkdir()
        (core_dir / "miner_registry.json").write_text(
            json.dumps(_SAMPLE_REGISTRY),
            encoding="utf-8",
        )

        try:
            with mock.patch.object(sys, "frozen", True, create=True), \
                 mock.patch.object(sys, "_MEIPASS", fake_meipass, create=True):
                result = registry_loader._read_bundled()

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(len(result["miners"]), 2)
        finally:
            shutil.rmtree(fake_meipass, ignore_errors=True)


# ---------------------------------------------------------------------------
# Envelope verification tests
# ---------------------------------------------------------------------------


class TestVerifyEnvelope(unittest.TestCase):
    """Tests for _verify_envelope()."""

    def test_valid_envelope(self):
        envelope = _make_envelope()
        result = registry_loader._verify_envelope(envelope)
        self.assertIsNotNone(result)
        self.assertEqual(result["schema_version"], 1)

    def test_missing_sha256_field(self):
        envelope = _make_envelope()
        del envelope["sha256"]
        result = registry_loader._verify_envelope(envelope)
        self.assertIsNone(result)

    def test_missing_registry_field(self):
        envelope = _make_envelope()
        del envelope["registry"]
        result = registry_loader._verify_envelope(envelope)
        self.assertIsNone(result)

    def test_sha256_mismatch(self):
        envelope = _make_envelope(bad_sha=True)
        result = registry_loader._verify_envelope(envelope)
        self.assertIsNone(result)

    def test_unsupported_schema(self):
        bad_reg = {**_SAMPLE_REGISTRY, "schema_version": 99}
        envelope = _make_envelope(bad_reg)
        result = registry_loader._verify_envelope(envelope)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
