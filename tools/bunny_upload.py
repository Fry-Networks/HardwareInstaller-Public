"""Bunny CDN upload + rollback tool — BUILD-PIPELINE ONLY.

SECURITY BOUNDARY:
- This module is invoked exclusively by build_cli.py on FryStation.
- It is NEVER imported by installer_main.py, gui/, or any core/* module.
- It is NEVER bundled into the PyInstaller installer or updater exes.
- The Bunny Account API Key it references is a WRITE-side credential and
  must never enter any shipped artifact.

If you are reading this from inside a frozen FryHubSetup.exe or
frynetworks_installer.exe, that is a security incident — flag immediately.
"""

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

# --- Frozen-execution guard (belt-and-suspenders) ---
if getattr(sys, 'frozen', False):
    raise RuntimeError(
        "tools.bunny_upload must NEVER run from a frozen executable. "
        "It contains write-side build-pipeline credentials. "
        "If you are seeing this error from a shipped exe, that is a security incident."
    )

# --- Constants ---
_STORAGE_ZONE = "frynetworks-downloads"
_STORAGE_BASE = f"https://storage.bunnycdn.com/{_STORAGE_ZONE}"
_PULL_ZONE_BASE = "https://frynetworks-downloads.b-cdn.net"
_PURGE_URL = "https://api.bunny.net/purge"
_CDN_PREFIX = "frynetworks-installer"
_OP_REF = "op://FryFarm/bunny.net/API Key"


# --- URL helpers ---

def _storage_url(zone_path: str) -> str:
    """Build a storage URL with leading-slash and double-slash guards."""
    zone_path = zone_path.lstrip("/")
    if "//" in zone_path:
        raise ValueError(f"Suspicious double slash in path: {zone_path!r}")
    if ".." in zone_path.split("/"):
        raise ValueError(f"Path traversal attempt: {zone_path!r}")
    return f"{_STORAGE_BASE}/{zone_path}"


def _pullzone_url(zone_path: str) -> str:
    """Build a pull-zone (read-side) URL."""
    zone_path = zone_path.lstrip("/")
    return f"{_PULL_ZONE_BASE}/{zone_path}"


# --- Credential resolution ---

def _resolve_access_key() -> str:
    """Read Bunny Account API Key from 1Password. Never echoes the value."""
    result = subprocess.run(
        ["op", "read", _OP_REF],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to resolve {_OP_REF} from 1Password. "
            f"Confirm `op signin` is active and the reference exists."
        )
    key = result.stdout.strip()
    if not key:
        raise RuntimeError(f"Empty value at {_OP_REF}")
    return key


# --- SHA-256 ---

def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file (1 MB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --- HTTP primitives ---

def _request_with_retry(req: urllib.request.Request, *, timeout: int,
                        max_retries: int = 1) -> urllib.response.addinfourl:
    """Execute a urllib Request with retry logic.

    Retry on connection errors, timeouts, and 5xx.
    Never retry on 401/403 (credential issue) or other 4xx.
    """
    last_exc = None
    for attempt in range(1 + max_retries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise RuntimeError(
                    f"HTTP {exc.code} at {req.full_url} — credential rejected, not retrying"
                ) from exc
            if 400 <= exc.code < 500:
                raise RuntimeError(
                    f"HTTP {exc.code} at {req.full_url}"
                ) from exc
            # 5xx — retry
            last_exc = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc
        if attempt < max_retries:
            time.sleep(2)
    raise RuntimeError(
        f"Request failed after {1 + max_retries} attempts: {req.full_url} — {last_exc}"
    )


def _put(zone_path: str, data: bytes, content_type: str, access_key: str) -> None:
    """PUT data to storage.bunnycdn.com/<zone>/<zone_path>."""
    url = _storage_url(zone_path)
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={
            "AccessKey": access_key,
            "Content-Type": content_type,
        },
    )
    resp = _request_with_retry(req, timeout=120)
    resp.close()


def _head(zone_path: str, access_key: str) -> bool:
    """HEAD-check whether a file exists at storage.bunnycdn.com/<zone>/<zone_path>."""
    url = _storage_url(zone_path)
    req = urllib.request.Request(
        url, method="HEAD",
        headers={"AccessKey": access_key},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        resp.close()
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def _purge(pull_zone_path: str, access_key: str) -> None:
    """Purge a single pull-zone URL from Bunny's CDN cache."""
    target_url = _pullzone_url(pull_zone_path)
    purge_url = f"{_PURGE_URL}?url={urllib.parse.quote(target_url, safe='')}"
    req = urllib.request.Request(
        purge_url, method="POST", data=b"",
        headers={"AccessKey": access_key},
    )
    resp = _request_with_retry(req, timeout=10)
    resp.close()


# --- Semver helper (standalone — no coupling to build_cli or installer_main) ---

def _semver_tuple(v: str) -> tuple:
    """Parse 'X.Y.Z' or 'vX.Y.Z' into (X, Y, Z) ints."""
    parts = v.lstrip("v").split("-", 1)[0].split(".", 3)
    return tuple(int(p) for p in parts)


# --- Public API ---

def upload_hub(version: str, exe_path: Path, *, min_required: Optional[str] = None,
               force: bool = False) -> None:
    """Upload FryHubSetup exe + manifest to Bunny CDN.

    Ordering: archive → latest exe → manifest → purge.
    Each step leaves the system in a recoverable state.
    """
    # 1. Pre-flight
    if not exe_path.exists():
        raise FileNotFoundError(f"Setup exe missing: {exe_path}")
    sha = _sha256_file(exe_path)
    size = exe_path.stat().st_size
    print(f"[upload-hub] {exe_path.name} ({size:,} bytes, sha256={sha[:16]}...)")

    # 2. Resolve credential (in-memory only)
    key = _resolve_access_key()

    # 3. Idempotency check
    latest_path = f"{_CDN_PREFIX}/hub/latest/{exe_path.name}"
    archive_path = f"{_CDN_PREFIX}/hub/archive/{exe_path.name}"
    if _head(latest_path, key) and not force:
        raise RuntimeError(
            f"{exe_path.name} already exists at /hub/latest/. "
            f"Pass --force to overwrite."
        )

    # 4. Read file into memory once (~80 MB)
    data = exe_path.read_bytes()

    # 5. Upload to /hub/archive/ FIRST
    _put(archive_path, data, "application/octet-stream", key)
    print(f"[upload-hub] uploaded archive: {archive_path}")

    # 6. Upload to /hub/latest/
    _put(latest_path, data, "application/octet-stream", key)
    print(f"[upload-hub] uploaded latest: {latest_path}")

    # 7. Generate manifest
    manifest = {
        "manifest_version": "1.0",
        "hub_version": version,
        "setup_url": _pullzone_url(latest_path),
        "setup_sha256": sha,
    }
    if min_required:
        manifest["min_required"] = min_required
    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")

    # 8. Upload manifest LAST (atomic publish-point)
    manifest_path = f"{_CDN_PREFIX}/hub/latest/fryhub_version.json"
    _put(manifest_path, manifest_bytes, "application/json", key)
    print(f"[upload-hub] uploaded manifest: {manifest_path}")

    # 9. Purge pull-zone cache
    _purge(latest_path, key)
    _purge(manifest_path, key)
    print("[upload-hub] purged CDN cache")

    print(f"[upload-hub] DONE — {exe_path.name} live at {_pullzone_url(latest_path)}")


def rollback_hub(target_version: str) -> None:
    """Rewrite hub manifest to point at an archived version.

    Downloads the archive exe to compute its sha256 (never trusts input).
    Refuses to 'rollback' to a version >= current WINDOWS_VERSION.
    """
    # 1. Version safety check
    from version import WINDOWS_VERSION
    if _semver_tuple(target_version) >= _semver_tuple(WINDOWS_VERSION):
        raise RuntimeError(
            f"Rollback target {target_version} is not older than current "
            f"{WINDOWS_VERSION}. Refusing to disguise an upgrade as a rollback."
        )

    # 2. Resolve credential
    key = _resolve_access_key()

    # 3. Confirm archive exists
    archive_filename = f"FryHubSetup-{target_version}.exe"
    archive_zone_path = f"{_CDN_PREFIX}/hub/archive/{archive_filename}"
    if not _head(archive_zone_path, key):
        raise FileNotFoundError(
            f"Archive does not exist: {_pullzone_url(archive_zone_path)}"
        )

    # 4. Download archive from pull zone (public, no auth) to compute sha256
    archive_url = _pullzone_url(archive_zone_path)
    tmp_path = Path(tempfile.mktemp(
        prefix=f"rollback-{target_version}-", suffix=".exe",
    ))
    try:
        req = urllib.request.Request(archive_url)
        with urllib.request.urlopen(req, timeout=120) as resp, \
             open(tmp_path, "wb") as out:
            shutil.copyfileobj(resp, out)

        # 5. Compute sha256
        sha = _sha256_file(tmp_path)
        dl_size = tmp_path.stat().st_size
        print(
            f"[rollback-hub] {archive_filename} "
            f"sha256={sha[:16]}... (size={dl_size:,})"
        )

        # 6. Rewrite manifest pointing at archive URL
        manifest = {
            "manifest_version": "1.0",
            "hub_version": target_version,
            "setup_url": archive_url,
            "setup_sha256": sha,
        }
        manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        manifest_path = f"{_CDN_PREFIX}/hub/latest/fryhub_version.json"

        # 7. Upload manifest
        _put(manifest_path, manifest_bytes, "application/json", key)
        print("[rollback-hub] manifest rewritten")

        # 8. Purge
        _purge(manifest_path, key)
        print("[rollback-hub] purged CDN cache")

        print(f"[rollback-hub] DONE — manifest now points at {target_version}")
    finally:
        # 9. Cleanup
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
