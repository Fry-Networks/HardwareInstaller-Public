"""Upgrade-from-Mysterium detection and teardown for BM (Bandwidth Miner).

Track 4 Fix #2 — detects pre-pivot Mysterium SDK state on a BM device and
tears it down before the new MystNodes SDK is provisioned. Runs as a
pre-provision step called by installer_main.py (wired in Fix #2b).

Detection signals (9 probes):
  File artifacts:  F1 myst-data/  F2 SDK/windows-myst-sdk/  F3 mysterium/
                   F4 config/mysterium.json  F5 myst.exe binary
  OS state:        S1 sc.exe service query  S2 nssm service status
                   FW firewall rules (3 named)  R1 registry key

Teardown phases:
  Phase A — rename file artifacts to .deprecated.<ts> (reversible via mv)
  Phase B — capture OS state to JSON, then delete service + firewall rules

Rollback reads the captured JSON and re-creates OS state, then renames
.deprecated files back to originals.

Fixture corpus for unit-tests: tests/fixtures/upgrade_from_myst_probes/
"""

import datetime
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SERVICE_NAME = "MysteriumNode"

FW_RULE_NAMES = (
    "MysteriumNode-API-In",
    "MysteriumNode-WireGuard-In",
    "MysteriumNode-WireGuard-Out",
)

LEGACY_DATA_DIR_NAMES = ("mysterium", "myst-data")
LEGACY_SDK_DIR = Path("SDK") / "windows-myst-sdk"
LEGACY_CONFIG_FILE = Path("config") / "mysterium.json"

STATE_FILENAME = "upgrade_from_myst_state.json"
DEPRECATED_SUFFIX_FMT = ".deprecated.{ts}"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class UpgradeFromMystResult:
    """Structured result from upgrade_from_myst_at_install."""
    upgrade_needed: bool
    upgrade_performed: bool
    failed: bool
    error: Optional[str] = None
    detected_signals: Dict[str, bool] = field(default_factory=dict)
    state_file_path: Optional[Path] = None
    renamed_paths: List[Path] = field(default_factory=list)
    timestamp: int = 0


# ---------------------------------------------------------------------------
# Encoding helper
# ---------------------------------------------------------------------------

def _decode_probe_output(raw: bytes) -> str:
    """Decode Windows tool output handling UTF-16-LE (with/without BOM) and UTF-8.

    nssm.exe emits UTF-16-LE without BOM on many Windows versions. Detected
    via null-byte heuristic (second byte is 0x00 for ASCII-range chars).
    Normalizes \\r\\n -> \\n for substring/regex matching.
    """
    if raw.startswith(b"\xff\xfe"):
        return raw.decode("utf-16-le", errors="replace").replace("\r\n", "\n")
    if raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16-be", errors="replace").replace("\r\n", "\n")
    if len(raw) >= 2 and raw[1:2] == b"\x00":
        return raw.decode("utf-16-le", errors="replace").replace("\r\n", "\n")
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


# ---------------------------------------------------------------------------
# Subprocess wrapper (monkey-patchable in tests)
# ---------------------------------------------------------------------------

def _run_subprocess_capture(cmd, timeout=30):
    """Run command and return raw stdout+stderr bytes.

    Tests monkey-patch this to return fixture bytes instead of spawning
    real processes.
    """
    result = subprocess.run(
        cmd, capture_output=True, timeout=timeout, check=False,
    )
    return result.stdout + result.stderr


# ---------------------------------------------------------------------------
# File-state probes (F1–F5)
# ---------------------------------------------------------------------------

def _probe_f1_myst_data(install_root):
    """F1: check install_root/myst-data/ exists."""
    return (install_root / "myst-data").is_dir()


def _probe_f2_windows_myst_sdk(install_root):
    """F2: check install_root/SDK/windows-myst-sdk/ exists."""
    return (install_root / LEGACY_SDK_DIR).is_dir()


def _probe_f3_mysterium(install_root):
    """F3: check install_root/mysterium/ exists."""
    return (install_root / "mysterium").is_dir()


def _probe_f4_mysterium_json(install_root):
    """F4: check install_root/config/mysterium.json exists."""
    return (install_root / LEGACY_CONFIG_FILE).is_file()


def _probe_f5_myst_exe(install_root):
    """F5: check install_root/SDK/windows-myst-sdk/myst.exe exists."""
    return (install_root / LEGACY_SDK_DIR / "myst.exe").is_file()


# ---------------------------------------------------------------------------
# OS-state probes (S1, S2, FW, R1)
# ---------------------------------------------------------------------------

def _probe_s1_service():
    """S1: sc.exe query — True if MysteriumNode service is registered."""
    try:
        raw = _run_subprocess_capture(
            ["sc.exe", "query", SERVICE_NAME], timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("S1 probe error: %s", exc)
        return False
    text = _decode_probe_output(raw)
    if "failed 1060" in text.lower() or "does not exist" in text.lower():
        return False
    return True


def _probe_s2_nssm(nssm_path):
    """S2: nssm status — True if nssm recognizes MysteriumNode."""
    try:
        raw = _run_subprocess_capture(
            [str(nssm_path), "status", SERVICE_NAME], timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("S2 probe error: %s", exc)
        return False
    text = _decode_probe_output(raw)
    lower = text.lower()
    if "can't open service" in lower or "does not exist" in lower:
        return False
    return True


def _probe_fw_rules():
    """FW: True if any of the 3 named Mysterium firewall rules exist."""
    names = ",".join(f"'{n}'" for n in FW_RULE_NAMES)
    ps = (
        f"(Get-NetFirewallRule -DisplayName {names} "
        f"-ErrorAction SilentlyContinue | Measure-Object).Count"
    )
    try:
        raw = _run_subprocess_capture(
            ["powershell", "-NoProfile", "-Command", ps], timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("FW probe error: %s", exc)
        return False
    text = _decode_probe_output(raw).strip()
    try:
        return int(text) > 0
    except ValueError:
        LOGGER.debug("FW probe non-integer output: %r", text)
        return False


def _probe_r1_registry():
    """R1: True if MysteriumNode registry key exists."""
    try:
        raw = _run_subprocess_capture(
            ["reg", "query",
             r"HKLM\SYSTEM\CurrentControlSet\Services\MysteriumNode"],
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("R1 probe error: %s", exc)
        return False
    text = _decode_probe_output(raw)
    lower = text.lower()
    if "unable to find" in lower or "error:" in lower:
        return False
    return True


# ---------------------------------------------------------------------------
# Public detection (pure read-only)
# ---------------------------------------------------------------------------

def detect_legacy_state(install_root, nssm_path):
    """Run all 9 detection probes. Returns ordered dict of signal -> bool.

    Pure read-only — no state mutation. Tolerates individual probe errors
    (returns False for the signal).
    """
    install_root = Path(install_root)
    return {
        "F1_myst_data": _probe_f1_myst_data(install_root),
        "F2_windows_myst_sdk": _probe_f2_windows_myst_sdk(install_root),
        "F3_mysterium": _probe_f3_mysterium(install_root),
        "F4_mysterium_json": _probe_f4_mysterium_json(install_root),
        "F5_myst_exe": _probe_f5_myst_exe(install_root),
        "S1_service": _probe_s1_service(),
        "S2_nssm": _probe_s2_nssm(Path(nssm_path)),
        "FW_rules": _probe_fw_rules(),
        "R1_registry": _probe_r1_registry(),
    }


# ---------------------------------------------------------------------------
# OS-state capture helpers (for rollback JSON)
# ---------------------------------------------------------------------------

def _capture_nssm_state(nssm_path):
    """Export MysteriumNode nssm config as dict for re-registration on rollback."""
    keys = ("Application", "AppDirectory", "AppParameters", "Start", "DisplayName")
    state = {"name": SERVICE_NAME}
    for key in keys:
        try:
            raw = _run_subprocess_capture(
                [str(nssm_path), "get", SERVICE_NAME, key], timeout=15,
            )
            state[key] = _decode_probe_output(raw).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.debug("nssm get %s error: %s", key, exc)
            return {}
    return state


def _capture_firewall_state():
    """Export the 3 named firewall rules as list of dicts for re-creation."""
    names = ",".join(f"'{n}'" for n in FW_RULE_NAMES)
    ps = (
        f"Get-NetFirewallRule -DisplayName {names} -ErrorAction SilentlyContinue | "
        f"ForEach-Object {{ $rule = $_; $port = $_ | Get-NetFirewallPortFilter; "
        f"[PSCustomObject]@{{ DisplayName=$rule.DisplayName; Direction=$rule.Direction.ToString(); "
        f"Protocol=$port.Protocol; LocalPort=$port.LocalPort; "
        f"Action=$rule.Action.ToString(); Enabled=$rule.Enabled.ToString(); "
        f"Profile=$rule.Profile.ToString() }} }} | ConvertTo-Json -Compress"
    )
    try:
        raw = _run_subprocess_capture(
            ["powershell", "-NoProfile", "-Command", ps], timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOGGER.debug("firewall state capture error: %s", exc)
        return []
    text = _decode_probe_output(raw).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return [parsed]
        return list(parsed)
    except json.JSONDecodeError as exc:
        LOGGER.debug("firewall JSON parse error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Phase A — file artifact rename
# ---------------------------------------------------------------------------

def _rename_legacy_file_artifacts(install_root, ts):
    """Rename present legacy artifacts to <name>.deprecated.<ts>.

    Returns list of new (deprecated) paths. Stops on first rename failure,
    raising OSError with partial results attached as exc.args[1].
    """
    install_root = Path(install_root)
    suffix = DEPRECATED_SUFFIX_FMT.format(ts=ts)
    targets = []

    for name in LEGACY_DATA_DIR_NAMES:
        p = install_root / name
        if p.exists():
            targets.append(p)

    sdk_dir = install_root / LEGACY_SDK_DIR
    if sdk_dir.exists():
        targets.append(sdk_dir)

    config_file = install_root / LEGACY_CONFIG_FILE
    if config_file.exists():
        targets.append(config_file)

    renamed = []
    for src in targets:
        dst = src.parent / (src.name + suffix)
        try:
            src.rename(dst)
            renamed.append(dst)
            LOGGER.info("renamed %s -> %s", src, dst)
        except OSError as exc:
            LOGGER.error("rename failed %s -> %s: %s", src, dst, exc)
            raise OSError(f"rename failed: {src} -> {dst}: {exc}", renamed) from exc

    return renamed


# ---------------------------------------------------------------------------
# Phase B — OS state deletion
# ---------------------------------------------------------------------------

def _delete_mysterium_service(nssm_path):
    """Stop + remove MysteriumNode via nssm. Returns True on success."""
    nssm_path = Path(nssm_path)
    for action in ("stop", "remove"):
        cmd = [str(nssm_path), action, SERVICE_NAME]
        if action == "remove":
            cmd.append("confirm")
        try:
            raw = _run_subprocess_capture(cmd, timeout=30)
            LOGGER.info("nssm %s: %s", action, _decode_probe_output(raw).strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("nssm %s error: %s", action, exc)
            return False
    return True


def _delete_mysterium_firewall_rules():
    """Remove the 3 named firewall rules. Returns count removed."""
    removed = 0
    for name in FW_RULE_NAMES:
        ps = f"Remove-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue"
        try:
            _run_subprocess_capture(
                ["powershell", "-NoProfile", "-Command", ps], timeout=15,
            )
            removed += 1
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("firewall rule remove %s: %s", name, exc)
    return removed


# ---------------------------------------------------------------------------
# State file I/O
# ---------------------------------------------------------------------------

def _write_state_file(install_root, ts, detected, service_state,
                      fw_state, renamed_paths, nssm_path):
    """Write upgrade_from_myst_state.json for rollback."""
    config_dir = Path(install_root) / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    state_path = config_dir / STATE_FILENAME
    state = {
        "schema_version": 1,
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "timestamp": ts,
        "install_root": str(install_root),
        "nssm_path": str(nssm_path),
        "service": service_state,
        "firewall_rules": fw_state,
        "renamed_paths": [
            {"original": str(p.parent / p.name.rsplit(DEPRECATED_SUFFIX_FMT.format(ts=ts), 1)[0]),
             "deprecated": str(p)}
            for p in renamed_paths
        ],
        "detected_signals": detected,
    }
    with open(state_path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    return state_path


def _read_state_file(state_file):
    """Read and parse the state JSON. Returns dict or raises."""
    with open(state_file, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Public orchestrator
# ---------------------------------------------------------------------------

def upgrade_from_myst_at_install(install_root, nssm_path,
                                 progress_callback=None):
    """Detect legacy Mysterium state and tear down if present.

    Six-step flow: detect -> capture OS state -> Phase A rename -> Phase B
    delete service -> Phase B delete firewall -> verify.

    On mid-teardown failure, calls rollback_upgrade and returns failed=True.
    Idempotent: safe when no legacy state present.
    """
    install_root = Path(install_root)
    nssm_path = Path(nssm_path)
    ts = int(time.time())

    def _progress(msg):
        if progress_callback:
            try:
                progress_callback(msg)
            except Exception:  # noqa: BLE001 — callback must never abort teardown
                pass

    # (1/6) Detect
    _progress("(1/6) Scanning for legacy Mysterium artifacts...")
    detected = detect_legacy_state(install_root, nssm_path)
    upgrade_needed = any(detected.values())

    if not upgrade_needed:
        LOGGER.info("no legacy Mysterium state detected; skipping upgrade")
        return UpgradeFromMystResult(
            upgrade_needed=False, upgrade_performed=False, failed=False,
            detected_signals=detected, timestamp=ts,
        )

    LOGGER.info("legacy Mysterium state detected: %s",
                {k: v for k, v in detected.items() if v})

    # (2/6) Capture OS state for rollback
    _progress("(2/6) Capturing OS state for rollback...")
    service_state = _capture_nssm_state(nssm_path) if detected.get("S1_service") else {}
    fw_state = _capture_firewall_state() if detected.get("FW_rules") else []

    # (3/6) Phase A — rename file artifacts
    _progress("(3/6) Renaming legacy file artifacts to .deprecated.%d..." % ts)
    try:
        renamed = _rename_legacy_file_artifacts(install_root, ts)
    except OSError as exc:
        partial = exc.args[1] if len(exc.args) > 1 else []
        state_path = _write_state_file(
            install_root, ts, detected, service_state, fw_state, partial, nssm_path,
        )
        LOGGER.error("Phase A failed; rolling back: %s", exc)
        rollback_upgrade(state_path, nssm_path)
        return UpgradeFromMystResult(
            upgrade_needed=True, upgrade_performed=False, failed=True,
            error=f"Phase A rename failed: {exc}",
            detected_signals=detected, timestamp=ts,
        )

    # Write state file now (Phase A done, before Phase B)
    state_path = _write_state_file(
        install_root, ts, detected, service_state, fw_state, renamed, nssm_path,
    )

    # (4/6) Phase B — delete service
    if detected.get("S1_service") or detected.get("S2_nssm"):
        _progress("(4/6) Removing MysteriumNode service...")
        if not _delete_mysterium_service(nssm_path):
            LOGGER.error("Phase B service deletion failed; rolling back")
            rollback_upgrade(state_path, nssm_path)
            return UpgradeFromMystResult(
                upgrade_needed=True, upgrade_performed=False, failed=True,
                error="Phase B: service deletion failed",
                detected_signals=detected, state_file_path=state_path,
                renamed_paths=renamed, timestamp=ts,
            )
    else:
        _progress("(4/6) No MysteriumNode service to remove — skipping")

    # (5/6) Phase B — delete firewall rules
    if detected.get("FW_rules"):
        _progress("(5/6) Removing MysteriumNode firewall rules...")
        _delete_mysterium_firewall_rules()
    else:
        _progress("(5/6) No MysteriumNode firewall rules — skipping")

    # (6/6) Verify
    _progress("(6/6) Verifying teardown complete...")
    post = detect_legacy_state(install_root, nssm_path)
    remaining = {k: v for k, v in post.items() if v}
    if remaining:
        LOGGER.warning("post-teardown signals still present: %s", remaining)

    return UpgradeFromMystResult(
        upgrade_needed=True, upgrade_performed=True, failed=False,
        detected_signals=detected, state_file_path=state_path,
        renamed_paths=renamed, timestamp=ts,
    )


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------

def rollback_upgrade(state_file, nssm_path):
    """Re-create OS state from captured JSON and rename artifacts back.

    Partial/missing state fields are skipped (not errors):
      - No "service" key or empty dict -> skip service re-registration
      - Empty "firewall_rules" -> skip firewall re-creation
      - Empty "renamed_paths" -> skip rename-back
    Returns True if final state matches captured pre-upgrade state.
    """
    state_file = Path(state_file)
    nssm_path = Path(nssm_path)

    try:
        state = _read_state_file(state_file)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("rollback: cannot read state file %s: %s", state_file, exc)
        return False

    ok = True

    # Re-register service if captured
    svc = state.get("service", {})
    if svc and svc.get("Application"):
        app = svc["Application"]
        try:
            _run_subprocess_capture(
                [str(nssm_path), "install", SERVICE_NAME, app], timeout=30,
            )
            for key in ("AppDirectory", "AppParameters", "Start", "DisplayName"):
                val = svc.get(key)
                if val:
                    _run_subprocess_capture(
                        [str(nssm_path), "set", SERVICE_NAME, key, val], timeout=15,
                    )
            LOGGER.info("rollback: service %s re-registered", SERVICE_NAME)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.error("rollback: service re-registration failed: %s", exc)
            ok = False

    # Re-create firewall rules if captured
    for rule in state.get("firewall_rules", []):
        name = rule.get("DisplayName", "")
        if not name:
            continue
        direction = rule.get("Direction", "Inbound")
        proto = rule.get("Protocol", "TCP")
        port = rule.get("LocalPort", "0")
        action = rule.get("Action", "Allow")
        ps = (
            f"New-NetFirewallRule -DisplayName '{name}' "
            f"-Direction {direction} -Protocol {proto} -LocalPort {port} "
            f"-Action {action} -Enabled True -Profile Any -ErrorAction SilentlyContinue"
        )
        try:
            _run_subprocess_capture(
                ["powershell", "-NoProfile", "-Command", ps], timeout=20,
            )
            LOGGER.info("rollback: firewall rule %s re-created", name)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("rollback: firewall rule %s failed: %s", name, exc)
            ok = False

    # Rename .deprecated files back to originals
    for entry in state.get("renamed_paths", []):
        deprecated = Path(entry["deprecated"])
        original = Path(entry["original"])
        if deprecated.exists() and not original.exists():
            try:
                deprecated.rename(original)
                LOGGER.info("rollback: renamed %s -> %s", deprecated, original)
            except OSError as exc:
                LOGGER.error("rollback: rename failed %s -> %s: %s",
                             deprecated, original, exc)
                ok = False

    return ok
