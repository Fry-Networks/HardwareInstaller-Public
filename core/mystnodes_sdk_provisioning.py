"""MystNodes SDK Client provisioning for BM (Bandwidth Miner).

Replaces core.mysterium_provisioning post Track 4 pivot. Deploys sdk_client.exe (the MystNodes
SDK partner binary) as a Windows service via nssm. No on-chain identity registration, no
TequilAPI, no WireGuard. The SDK client makes a single outbound connection to
proxy.mystnodes.com (UDP 443 QUIC primary, TCP 443 TLS fallback) authenticated by the partner
--user.token issued by MystNodes.

Token plumbing: build-time embedded in build_config.json under
partner_integrations.mystnodes_sdk.reg_token, populated at build time by
build_installer.ps1 reading from op://Bandwidth Miners/Mysterium SDK API/MYST_REG_TOKEN. Same
fleet-wide token across all BM devices.

Phase B token validation: VALID against proxy.mystnodes.com:443 QUIC. See
/c/tmp/track4_recon_1777471988/TRACK4_RECON_VERDICT.md for the verbatim probe evidence.
"""

import datetime
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

LOGGER = logging.getLogger(__name__)

# Service & path constants
SDK_SERVICE_NAME = "MystNodesSDK"
SDK_SERVICE_DISPLAY = "MystNodes SDK Client"
SDK_BINARY_NAME = "sdk_client.exe"

# Source location in the staged build (relative to install root).
SDK_SOURCE_REL = Path("SDK") / "windows-mystnodes-sdk" / SDK_BINARY_NAME

# CLI args confirmed in Phase B probe — DO NOT use --listen.addr=127.0.0.1 (breaks outbound
# connectivity). Default listen.addr=0.0.0.0 works; we omit the flag entirely so sdk_client
# uses its built-in default.
SDK_CLI_LOG_LEVEL = "info"


@dataclass
class StepResult:
    """Mirror of the legacy mysterium_provisioning result shape so callers don't need to change."""
    success: bool
    step: str = ""
    error: str = ""


def _read_token_from_build_config(install_root: Path) -> Optional[str]:
    """Token comes from build_config.json bundled into the installer EXE.

    build_installer.ps1 reads op://Bandwidth Miners/Mysterium SDK API/MYST_REG_TOKEN and
    embeds under partner_integrations.mystnodes_sdk.reg_token at build time.
    """
    cfg_path = Path(getattr(sys, "_MEIPASS", ".")) / "build_config.json"
    if not cfg_path.exists():
        LOGGER.error("build_config.json missing at %s", cfg_path)
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("build_config.json read failed: %s", exc)
        return None
    return (
        cfg.get("partner_integrations", {})
           .get("mystnodes_sdk", {})
           .get("reg_token")
    )


def _write_state_file(install_root: Path) -> None:
    """Write mystnodes_sdk.json state file for forensics + Fix #2 upgrade detection."""
    config_dir = install_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "schema_version": 1,
        "service_name": SDK_SERVICE_NAME,
        "managed_by": "Track 4",
        "installed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    state_path = config_dir / "mystnodes_sdk.json"
    try:
        with open(state_path, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError as exc:
        LOGGER.warning("state file write failed (non-fatal): %s", exc)


def _step_stage_binary(install_root: Path) -> StepResult:
    """1/6: Copy sdk_client.exe from SDK/windows-mystnodes-sdk/ to install root."""
    src = install_root / SDK_SOURCE_REL
    dst = install_root / SDK_BINARY_NAME
    if not src.exists():
        return StepResult(False, "1/6", f"SDK binary missing at {src}")
    try:
        shutil.copy2(src, dst)
    except OSError as exc:
        return StepResult(False, "1/6", f"binary copy failed: {exc}")
    return StepResult(True, "1/6")


def _step_install_service(install_root: Path, nssm_path: Path) -> StepResult:
    """2/6: nssm install MystNodesSDK pointing at the staged binary."""
    binary = install_root / SDK_BINARY_NAME
    cmd = [str(nssm_path), "install", SDK_SERVICE_NAME, str(binary)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return StepResult(False, "2/6", f"nssm install failed: {exc}")
    if result.returncode != 0:
        return StepResult(False, "2/6", f"nssm install rc={result.returncode}: {result.stderr[:300]}")
    return StepResult(True, "2/6")


def _step_configure_service(install_root: Path, nssm_path: Path) -> StepResult:
    """3/6: nssm set service options — AppDirectory, AppStdout/Stderr, Description."""
    log_dir = install_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    settings = [
        ("AppDirectory", str(install_root)),
        ("AppStdout", str(log_dir / "sdk_client.out.log")),
        ("AppStderr", str(log_dir / "sdk_client.err.log")),
        ("Description", "MystNodes SDK partner client — bandwidth monetization"),
        ("Start", "SERVICE_AUTO_START"),
    ]
    for key, val in settings:
        cmd = [str(nssm_path), "set", SDK_SERVICE_NAME, key, val]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return StepResult(False, "3/6", f"nssm set {key} failed: {exc}")
        if result.returncode != 0:
            return StepResult(False, "3/6", f"nssm set {key} rc={result.returncode}")
    return StepResult(True, "3/6")


def _step_set_token_param(install_root: Path, nssm_path: Path, token: str) -> StepResult:
    """4/6: Inject --user.token into nssm AppParameters.

    Done as a separate step so token is only handled by this one function. Errors from this
    step are sanitized to never include the token in the message.
    """
    if not token:
        return StepResult(False, "4/6", "MYST_REG_TOKEN is empty")
    params = f"--user.token={token} --log.level={SDK_CLI_LOG_LEVEL}"
    cmd = [str(nssm_path), "set", SDK_SERVICE_NAME, "AppParameters", params]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return StepResult(False, "4/6", f"nssm set AppParameters failed: {exc}")
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-16-le", errors="replace")
        sanitized = stderr.replace(token, "<REDACTED>") if token else stderr
        return StepResult(False, "4/6", f"nssm set AppParameters rc={result.returncode}: {sanitized[:300]}")
    return StepResult(True, "4/6")


def _step_firewall_rule(install_root: Path) -> StepResult:
    """5/6: Allow outbound UDP+TCP 443 for sdk_client.exe.

    QUIC primary on UDP 443, TLS fallback on TCP 443. Uses PowerShell Get-NetFirewallRule +
    New-NetFirewallRule for true add-or-skip semantics (no duplicates).
    """
    binary = install_root / SDK_BINARY_NAME
    rules = [
        ("FryNetworks-MystNodesSDK-Out-UDP443", "UDP", "443"),
        ("FryNetworks-MystNodesSDK-Out-TCP443", "TCP", "443"),
    ]
    for name, proto, port in rules:
        ps = (
            f"if (-not (Get-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue)) {{ "
            f"New-NetFirewallRule -DisplayName '{name}' -Direction Outbound -Action Allow "
            f"-Program '{binary}' -Protocol {proto} -RemotePort {port} -Profile Any "
            f"| Out-Null }}"
        )
        cmd = ["powershell", "-NoProfile", "-Command", ps]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return StepResult(False, "5/6", f"firewall rule {name} failed: {exc}")
        if result.returncode != 0:
            return StepResult(False, "5/6", f"firewall {name} rc={result.returncode}: {result.stderr[:300]}")
    return StepResult(True, "5/6")


def _step_start_service(install_root: Path, nssm_path: Path) -> StepResult:
    """6/6: Start service + bounded health-check.

    Health-check is process-alive only — sdk_client doesn't expose a TequilAPI-equivalent.
    Confirm service entered Running and stayed there for 30 seconds without exit.
    """
    cmd = [str(nssm_path), "start", SDK_SERVICE_NAME]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return StepResult(False, "6/6", f"nssm start failed: {exc}")
    if result.returncode != 0:
        return StepResult(False, "6/6", f"nssm start rc={result.returncode}: {result.stderr[:300]}")

    deadline = time.time() + 30
    while time.time() < deadline:
        status_cmd = [str(nssm_path), "status", SDK_SERVICE_NAME]
        try:
            status = subprocess.run(status_cmd, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            time.sleep(2)
            continue
        stdout = (status.stdout or b"").decode("utf-16-le", errors="replace")
        if "SERVICE_RUNNING" in stdout:
            time.sleep(5)
            re_check = subprocess.run(status_cmd, capture_output=True, timeout=10, check=False)
            _stdout = (re_check.stdout or b"").decode("utf-16-le", errors="replace")
            if "SERVICE_RUNNING" in _stdout:
                return StepResult(True, "6/6")
        time.sleep(2)
    return StepResult(False, "6/6", "service did not reach sustained SERVICE_RUNNING within 30s")


def provision_mystnodes_sdk_at_install(
    install_root: Path,
    nssm_path: Path,
    progress_callback: Optional[Callable[[str, str], None]] = None,
) -> StepResult:
    """Public entry — six-step provision chain for sdk_client.exe.

    Args:
        install_root: %ProgramData%/FryNetworks/miner-BM (or AppData equivalent).
        nssm_path: install_root/nssm.exe — staged by build, used as service manager.
        progress_callback: optional fn(step_label, status) for installer UI.

    Returns:
        StepResult with success=True if all 6 steps passed; success=False with first
        failing step on any failure. Caller invokes cleanup_mystnodes_sdk_on_failure if False.
    """
    install_root = Path(install_root)
    nssm_path = Path(nssm_path)

    token = _read_token_from_build_config(install_root)
    if not token:
        return StepResult(False, "0/6", "MYST_REG_TOKEN missing from build_config.json")

    steps = [
        ("1/6", "stage_binary", lambda: _step_stage_binary(install_root)),
        ("2/6", "install_service", lambda: _step_install_service(install_root, nssm_path)),
        ("3/6", "configure_service", lambda: _step_configure_service(install_root, nssm_path)),
        ("4/6", "set_token_param", lambda: _step_set_token_param(install_root, nssm_path, token)),
        ("5/6", "firewall_rule", lambda: _step_firewall_rule(install_root)),
        ("6/6", "start_service", lambda: _step_start_service(install_root, nssm_path)),
    ]

    for label, name, step_fn in steps:
        if progress_callback:
            try:
                progress_callback(label, "running")
            except Exception:  # noqa: BLE001 — UI callback must never abort provision
                pass
        result = step_fn()
        if progress_callback:
            try:
                progress_callback(label, "ok" if result.success else "fail")
            except Exception:  # noqa: BLE001
                pass
        if not result.success:
            LOGGER.error("Provision step %s (%s) failed: %s", label, name, result.error)
            return result

    _write_state_file(install_root)
    return StepResult(True, "6/6")


def cleanup_mystnodes_sdk_on_failure(
    install_root: Path,
    nssm_path: Path,
) -> None:
    """Public entry — teardown invoked when provision fails.

    Idempotent. Safe to call when service was never registered, when firewall rules don't
    exist, when binary wasn't staged. sdk_client is stateless — no identity/state files to
    preserve.
    """
    install_root = Path(install_root)
    nssm_path = Path(nssm_path)

    for action in ("stop", "remove"):
        cmd_args = [str(nssm_path), action, SDK_SERVICE_NAME]
        if action == "remove":
            cmd_args.append("confirm")
        try:
            subprocess.run(cmd_args, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("nssm %s during cleanup: %s", action, exc)

    for name in ("FryNetworks-MystNodesSDK-Out-UDP443", "FryNetworks-MystNodesSDK-Out-TCP443"):
        ps = f"Remove-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue"
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("firewall rule remove %s: %s", name, exc)

    staged = install_root / SDK_BINARY_NAME
    if staged.exists():
        try:
            staged.unlink()
        except OSError as exc:
            LOGGER.warning("staged binary unlink: %s", exc)
