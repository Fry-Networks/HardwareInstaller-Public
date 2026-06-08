"""Docker Desktop auto-installation for RDN and other container-dependent miners.

Provides idempotent Docker detection, WSL2 enablement, Docker Desktop download
and silent installation, and post-install readiness polling.

A Docker install failure should NOT prevent the rest of the miner installation
from proceeding. Callers must treat this module as best-effort.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

_logger = logging.getLogger(__name__)

_DOCKER_DESKTOP_URL = "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe"


def _which(cmd: str) -> Optional[str]:
    """Find *cmd* in PATH, returning the absolute path or None."""
    return shutil.which(cmd)


def is_docker_installed() -> bool:
    """Return True if the ``docker`` CLI is on PATH and responds to ``--version``."""
    docker_exe = _which("docker")
    if not docker_exe:
        return False
    try:
        result = subprocess.run(
            [docker_exe, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and "Docker" in result.stdout
    except Exception:
        return False


def is_docker_running() -> bool:
    """Return True if the Docker daemon is reachable (``docker info`` succeeds)."""
    docker_exe = _which("docker")
    if not docker_exe:
        return False
    try:
        result = subprocess.run(
            [docker_exe, "info"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_wsl2() -> Tuple[bool, bool]:
    """Check WSL2 status and attempt to enable it if missing.

    Returns:
        (ready: bool, reboot_required: bool)
        - ready=True         → WSL2 is available now.
        - ready=False, reboot_required=True  → WSL2 was just enabled; reboot needed.
        - ready=False, reboot_required=False → WSL2 enablement failed.
    """
    # First check if WSL is already present and version 2
    try:
        result = subprocess.run(
            ["wsl", "--status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # WSL present if the command succeeds and mentions "Default Version: 2"
        if result.returncode == 0 and "Default Version: 2" in result.stdout:
            _logger.debug("WSL2 already enabled")
            return True, False
    except Exception:
        pass

    # Attempt to enable WSL2 + Virtual Machine Platform
    _logger.info("WSL2 not detected — enabling via wsl --install --no-distribution")
    try:
        result = subprocess.run(
            ["wsl", "--install", "--no-distribution"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        # Microsoft docs: a 0 returncode with "restart" / "reboot" in stderr
        # means the feature is staged and requires a reboot.
        out = (result.stdout or "") + (result.stderr or "")
        needs_reboot = any(
            keyword in out.lower()
            for keyword in ("restart", "reboot", "restart required", "reboot required")
        )
        if result.returncode == 0 and needs_reboot:
            _logger.info("WSL2 enabled but reboot required")
            return False, True
        if result.returncode == 0:
            # WSL might now be available without reboot on newer Windows builds
            return True, False
    except Exception as exc:
        _logger.warning("WSL2 enablement failed: %s", exc)

    return False, False


def download_docker_desktop(dest_dir: Path) -> Path:
    """Download Docker Desktop installer to *dest_dir*.

    Returns:
        Path to the downloaded installer executable.

    Raises:
        RuntimeError: if the download fails after retries.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "Docker Desktop Installer.exe"

    _logger.info("Downloading Docker Desktop installer...")
    for attempt in range(1, 4):
        try:
            with requests.get(_DOCKER_DESKTOP_URL, stream=True, timeout=120) as resp:
                resp.raise_for_status()
                with open(dest_path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            fh.write(chunk)
            _logger.info("Docker Desktop installer saved to %s", dest_path)
            return dest_path
        except Exception as exc:
            _logger.warning("Docker Desktop download attempt %d/3 failed: %s", attempt, exc)
            time.sleep(2 ** attempt)

    raise RuntimeError("Failed to download Docker Desktop installer after 3 attempts")


def install_docker_desktop(installer_path: Path) -> bool:
    """Run Docker Desktop silent installer.

    Uses ``install --quiet --accept-license --backend=wsl-2``.
    Waits for completion.

    Returns True on success, False otherwise.
    """
    installer_path = Path(installer_path)
    if not installer_path.exists():
        _logger.error("Docker Desktop installer not found: %s", installer_path)
        return False

    cmd = [
        str(installer_path),
        "install",
        "--quiet",
        "--accept-license",
        "--backend=wsl-2",
    ]
    _logger.info("Running Docker Desktop silent install...")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0:
            _logger.info("Docker Desktop installed successfully")
            return True
        _logger.error(
            "Docker Desktop install failed (rc=%d): stdout=%s stderr=%s",
            result.returncode,
            result.stdout,
            result.stderr,
        )
    except subprocess.TimeoutExpired:
        _logger.error("Docker Desktop installer timed out (>10 min)")
    except Exception as exc:
        _logger.error("Docker Desktop install error: %s", exc)

    return False


def wait_for_docker_ready(timeout_seconds: int = 120) -> bool:
    """Poll ``docker info`` until it succeeds or *timeout_seconds* elapse.

    Docker Desktop can take 30–90 seconds to start after installation.
    """
    docker_exe = _which("docker")
    if not docker_exe:
        return False

    deadline = time.monotonic() + timeout_seconds
    interval = 5
    _logger.info("Waiting up to %ds for Docker Desktop to become ready...", timeout_seconds)

    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [docker_exe, "info"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                _logger.info("Docker Desktop is ready")
                return True
        except Exception:
            pass
        time.sleep(interval)

    _logger.warning("Docker Desktop did not become ready within %ds", timeout_seconds)
    return False


def ensure_docker() -> Tuple[bool, str]:
    """Orchestrate Docker Desktop availability.

    Idempotent: if Docker is already installed and running, returns immediately.

    Returns:
        (success: bool, message: str)
        - (True, "already_installed")   → Docker was already present.
        - (True, "installed")           → Docker was installed and is ready.
        - (False, "reboot_required")    → WSL2 needs a reboot before Docker can work.
        - (False, "error: <reason>")    → Installation failed.
    """
    if is_docker_installed():
        if is_docker_running():
            return True, "already_installed"
        # Installed but not running — try to wait for it (maybe post-reboot)
        if wait_for_docker_ready(timeout_seconds=60):
            return True, "already_installed"
        _logger.warning("Docker installed but not running")
        # Fall through to re-installation attempt

    # Ensure WSL2 prerequisite
    wsl_ready, reboot_needed = ensure_wsl2()
    if reboot_needed:
        return False, "reboot_required"
    if not wsl_ready:
        _logger.warning("WSL2 not available — proceeding anyway, Docker install may fail")

    # Download and install
    try:
        temp_dir = Path(os.environ.get("TEMP", "/tmp"))
        installer_path = download_docker_desktop(temp_dir)
    except Exception as exc:
        return False, f"error: download failed — {exc}"

    if not install_docker_desktop(installer_path):
        return False, "error: Docker Desktop installation failed"

    if wait_for_docker_ready():
        return True, "installed"

    return False, "error: Docker Desktop installed but did not become ready"
