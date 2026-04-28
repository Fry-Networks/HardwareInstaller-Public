"""Install-time Mysterium provisioning worker chain.

Runs sequentially on the installer's background thread after SDK staging.
Each step retries up to 3 times with exponential backoff (1s, 3s, 9s).
An 8-minute global deadline aborts the entire chain if exceeded.

Lift sources (HardwareExe-git/miner_GUI/services/mysterium.py):
  - NSSM install:  lines 782-979  (_install_windows_service_via_nssm)
  - Firewall:      lines 203-259  (_ensure_firewall/wireguard_port_windows)
  - TequilAPI:     lines 308-762  (_register_node, _set_beneficiary, etc.)
"""

import json
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import requests

GLOBAL_DEADLINE_SECONDS = 480  # 8 minutes
TEQUILAPI_BASE = "http://127.0.0.1:4050"
WIREGUARD_PORT = 51820
WIREGUARD_PORT_RANGE = "51820:51820"
MAX_RETRIES = 3
BACKOFF_SECONDS = [1, 3, 9]

CREATE_NO_WINDOW = 0x08000000


@dataclass
class ProvisionResult:
    success: bool
    failed_step: Optional[str] = None
    error: Optional[str] = None


def provision_mysterium_at_install(
    base_dir: Path,
    nssm_path: Path,
    myst_bin: Path,
    data_dir: Path,
    payout_addr: str,
    reg_token: str,
    api_key: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> ProvisionResult:
    """Run the 14-step Mysterium provisioning chain.

    Must be called from a background thread (blocks for up to 8 minutes).
    *progress_callback* receives human-readable status strings and is safe
    to call from the background thread (caller wires it to Qt signals).
    """
    deadline = time.monotonic() + GLOBAL_DEADLINE_SECONDS
    identity: Optional[str] = None

    steps = [
        ("1/14", "network_precheck", lambda: _step_network_precheck()),
        ("2/14", "nssm_install", lambda: _step_nssm_install(nssm_path, myst_bin, data_dir)),
        ("3/14", "nssm_configure", lambda: _step_nssm_configure(nssm_path, data_dir)),
        ("4/14", "firewall_tequilapi", lambda: _step_firewall_tequilapi()),
        ("5/14", "firewall_wireguard", lambda: _step_firewall_wireguard()),
        ("6/14", "service_start", lambda: _step_service_start(nssm_path)),
        ("7/14", "daemon_ready", lambda: _step_daemon_ready()),
    ]

    # Run steps 1-7 (no TequilAPI data dependency)
    for label, name, fn in steps:
        result = _run_with_retry(label, name, fn, deadline, progress_callback)
        if not result.success:
            return result

    # Step 8: create identity — captures identity address for subsequent steps
    def _step8():
        nonlocal identity
        identity = _step_create_identity()
        return identity
    result = _run_with_retry("8/14", "create_identity", _step8, deadline, progress_callback)
    if not result.success:
        return result

    # Steps 9-14: TequilAPI configuration (depend on identity)
    tequil_steps = [
        ("9/14", "set_beneficiary", lambda: _step_set_beneficiary(identity, payout_addr)),
        ("10/14", "set_mmn", lambda: _step_set_mmn(api_key)),
        ("11/14", "register_node", lambda: _step_register_node(identity, reg_token)),
        ("12/14", "accept_terms", lambda: _step_accept_terms()),
        ("13/14", "start_wireguard", lambda: _step_start_service("wireguard")),
        ("14/14", "start_services", lambda: _step_start_additional_services()),
    ]

    for label, name, fn in tequil_steps:
        result = _run_with_retry(label, name, fn, deadline, progress_callback)
        if not result.success:
            return result

    return ProvisionResult(success=True)


def cleanup_mysterium_on_failure(base_dir: Path, nssm_path: Path) -> None:
    """Best-effort cleanup after provisioning failure.

    Removes the NSSM service and firewall rules.  Does NOT touch:
    SDK staging dir, myst-data/, mysterium.json (next install reuses them).
    """
    _run_quiet([str(nssm_path), "stop", "MysteriumNode"], timeout=10)
    _run_quiet([str(nssm_path), "remove", "MysteriumNode", "confirm"], timeout=10)
    _run_quiet([
        "netsh", "advfirewall", "firewall", "delete", "rule",
        'name=FryNetworks Mysterium API 4050',
    ], timeout=10)
    _run_quiet([
        "netsh", "advfirewall", "firewall", "delete", "rule",
        'name=FryNetworks Mysterium WireGuard UDP 51820 Inbound',
    ], timeout=10)
    _run_quiet([
        "netsh", "advfirewall", "firewall", "delete", "rule",
        'name=FryNetworks Mysterium WireGuard UDP 51820 Outbound',
    ], timeout=10)


# --------------- internal helpers ---------------


def _run_with_retry(
    label: str,
    name: str,
    fn: Callable,
    deadline: float,
    progress_callback: Optional[Callable[[str], None]],
) -> ProvisionResult:
    """Execute *fn* up to MAX_RETRIES times with exponential backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        if time.monotonic() > deadline:
            return ProvisionResult(
                success=False,
                failed_step=f"{label} {name} (deadline exceeded)",
                error="Global 8-minute deadline exceeded",
            )
        backoff = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
        if progress_callback:
            try:
                progress_callback(
                    f"step {label} {name} (attempt {attempt}/{MAX_RETRIES})"
                )
            except Exception:
                pass
        try:
            fn()
            return ProvisionResult(success=True)
        except Exception as exc:
            if attempt < MAX_RETRIES:
                if progress_callback:
                    try:
                        progress_callback(
                            f"step {label} {name} (attempt {attempt}/{MAX_RETRIES}, "
                            f"backoff {backoff}s): {exc}"
                        )
                    except Exception:
                        pass
                time.sleep(backoff)
            else:
                return ProvisionResult(
                    success=False,
                    failed_step=f"{label} {name}",
                    error=str(exc),
                )
    # Should not reach here
    return ProvisionResult(success=False, failed_step=f"{label} {name}", error="unknown")


def _run_quiet(args: list, timeout: int = 10) -> None:
    """Run a subprocess, ignoring all errors."""
    try:
        subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def _run_checked(args: list, timeout: int = 10) -> subprocess.CompletedProcess:
    """Run a subprocess and raise on non-zero exit."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"Command failed (rc={result.returncode}): {err}")
    return result


# --------------- step implementations ---------------


def _step_network_precheck() -> None:
    """Step 1: Verify outbound network connectivity."""
    try:
        s = socket.create_connection(("1.1.1.1", 443), timeout=3)
        s.close()
    except Exception:
        raise RuntimeError("No outbound network connectivity (1.1.1.1:443 unreachable)")


def _step_nssm_install(nssm_path: Path, myst_bin: Path, data_dir: Path) -> None:
    """Step 2: Install MysteriumNode as a Windows service via NSSM."""
    # Check if service already exists
    check = subprocess.run(
        [str(nssm_path), "status", "MysteriumNode"],
        capture_output=True, timeout=5, creationflags=CREATE_NO_WINDOW,
    )
    if check.returncode == 0:
        # Service exists — update parameters in place
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        desired = " ".join([
            f"--config-dir={data_dir}",
            f"--data-dir={data_dir}",
            f"--log-dir={log_dir}",
            "--tequilapi.address=127.0.0.1",
            "--tequilapi.port=4050",
            f"--udp.ports={WIREGUARD_PORT_RANGE}",
            "daemon",
        ])
        _run_checked([
            str(nssm_path), "set", "MysteriumNode", "Application", str(myst_bin),
        ], timeout=10)
        _run_checked([
            str(nssm_path), "set", "MysteriumNode", "AppParameters", desired,
        ], timeout=10)
        return

    # Fresh install
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    app_params = [
        f"--config-dir={data_dir}",
        f"--data-dir={data_dir}",
        f"--log-dir={log_dir}",
        "--tequilapi.address=127.0.0.1",
        "--tequilapi.port=4050",
        f"--udp.ports={WIREGUARD_PORT_RANGE}",
        "daemon",
    ]
    _run_checked([
        str(nssm_path), "install", "MysteriumNode", str(myst_bin), *app_params,
    ], timeout=20)


def _step_nssm_configure(nssm_path: Path, data_dir: Path) -> None:
    """Step 3: Configure NSSM service parameters."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    params = [
        ("AppDirectory", str(data_dir)),
        ("DisplayName", "Mysterium Node"),
        ("Description", "Mysterium Network VPN Node"),
        ("Start", "SERVICE_AUTO_START"),
        ("AppStdout", str(log_dir / "mysterium_service.out.log")),
        ("AppStderr", str(log_dir / "mysterium_service.err.log")),
    ]
    for param, value in params:
        _run_checked([
            str(nssm_path), "set", "MysteriumNode", param, value,
        ], timeout=5)


def _step_firewall_tequilapi() -> None:
    """Step 4: Add firewall rule for TequilAPI TCP 4050 inbound."""
    _run_checked([
        "netsh", "advfirewall", "firewall", "add", "rule",
        "name=FryNetworks Mysterium API 4050",
        "dir=in", "action=allow", "protocol=TCP", "localport=4050",
    ], timeout=10)


def _step_firewall_wireguard() -> None:
    """Step 5: Add firewall rules for WireGuard UDP 51820 inbound + outbound."""
    _run_checked([
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name=FryNetworks Mysterium WireGuard UDP {WIREGUARD_PORT} Inbound",
        "dir=in", "action=allow", "protocol=UDP", f"localport={WIREGUARD_PORT}",
    ], timeout=10)
    _run_checked([
        "netsh", "advfirewall", "firewall", "add", "rule",
        f"name=FryNetworks Mysterium WireGuard UDP {WIREGUARD_PORT} Outbound",
        "dir=out", "action=allow", "protocol=UDP", f"localport={WIREGUARD_PORT}",
    ], timeout=10)


def _step_service_start(nssm_path: Path) -> None:
    """Step 6: Start the MysteriumNode service."""
    _run_checked([str(nssm_path), "start", "MysteriumNode"], timeout=10)


def _step_daemon_ready() -> None:
    """Step 7: Poll TequilAPI /healthcheck until the daemon is ready (30s)."""
    end = time.monotonic() + 30
    last_err = ""
    while time.monotonic() < end:
        try:
            resp = requests.get(f"{TEQUILAPI_BASE}/healthcheck", timeout=3)
            if resp.status_code == 200:
                return
            last_err = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_err = str(exc)
        time.sleep(1)
    raise RuntimeError(f"Daemon not ready after 30s: {last_err}")


def _step_create_identity() -> str:
    """Step 8: Create (or retrieve) Mysterium identity via POST /identities."""
    # First try to get existing identity
    try:
        resp = requests.get(f"{TEQUILAPI_BASE}/identities", timeout=10)
        if resp.status_code == 200:
            identities = resp.json().get("identities", [])
            if identities:
                return identities[0]["id"]
    except Exception:
        pass

    # Create new identity
    resp = requests.post(
        f"{TEQUILAPI_BASE}/identities",
        json={"passphrase": ""},
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"POST /identities failed: HTTP {resp.status_code} {resp.text[:200]}")
    data = resp.json()
    ident = data.get("id")
    if not ident:
        raise RuntimeError(f"POST /identities returned no id: {data}")
    return ident


def _step_set_beneficiary(identity: str, payout_addr: str) -> None:
    """Step 9: Set payout beneficiary address."""
    resp = requests.put(
        f"{TEQUILAPI_BASE}/identities/{identity}/beneficiary",
        json={"beneficiary": payout_addr},
        timeout=10,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"PUT beneficiary failed: HTTP {resp.status_code} {resp.text[:200]}")


def _step_set_mmn(api_key: str) -> None:
    """Step 10: Set MMN API key."""
    resp = requests.post(
        f"{TEQUILAPI_BASE}/mmn/api-key",
        json={"api_key": api_key},
        timeout=10,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"POST mmn/api-key failed: HTTP {resp.status_code} {resp.text[:200]}")


def _step_register_node(identity: str, reg_token: str) -> None:
    """Step 11: Register node identity on the Mysterium network."""
    resp = requests.post(
        f"{TEQUILAPI_BASE}/identities/{identity}/register",
        json={"token": reg_token},
        timeout=30,
    )
    # 202 = accepted for async registration, 200 = already registered
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"POST register failed: HTTP {resp.status_code} {resp.text[:200]}")


def _step_accept_terms() -> None:
    """Step 12: Accept Mysterium provider and consumer terms."""
    resp = requests.post(
        f"{TEQUILAPI_BASE}/terms",
        json={
            "agreed_provider": True,
            "agreed_consumer": True,
        },
        timeout=5,
    )
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"POST /terms failed: HTTP {resp.status_code} {resp.text[:200]}")


def _step_start_service(service_type: str) -> None:
    """Step 13: Start a specific Mysterium service (e.g., wireguard)."""
    resp = requests.post(
        f"{TEQUILAPI_BASE}/services",
        json={
            "type": service_type,
            "provider_id": "",  # auto-detect
            "options": {},
        },
        timeout=10,
    )
    # 201 = created, 409 = already running (both OK)
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"POST /services ({service_type}) failed: HTTP {resp.status_code}")


def _step_start_additional_services() -> None:
    """Step 14: Start dvpn, data_transfer, scraping, monitoring services."""
    for svc in ("dvpn", "data_transfer", "scraping", "monitoring"):
        try:
            _step_start_service(svc)
        except Exception:
            # Non-fatal: additional services are best-effort
            pass
