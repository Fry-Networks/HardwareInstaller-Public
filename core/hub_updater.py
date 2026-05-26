"""
FryHub Update Utilities -- shared between launch-time check and GUI manual check.

Extracted from installer_main.py so both paths use identical manifest / compare /
download / launch logic.  No standalone updater dependency.
"""

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from core.hub_config import read_hub_config, write_hub_config
from version import WINDOWS_VERSION

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Manifest constants
# ---------------------------------------------------------------------------
_HUB_MANIFEST_URL = (
    "https://raw.githubusercontent.com/Fry-Foundation/"
    "HardwareInstaller-Public/main/fryhub_version.json"
)
_HUB_MANIFEST_REQUIRED = ("manifest_version", "hub_version", "setup_url", "setup_sha256")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_hub_manifest(timeout: int = 5) -> Optional[dict]:
    """GET fryhub_version.json from GitHub. Returns None on any failure."""
    try:
        req = urllib.request.Request(_HUB_MANIFEST_URL)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        import traceback as _diag2_tb
        import os as _diag2_os
        try:
            _diag2_os.makedirs(r'C:\temp', exist_ok=True)
            with open(r'C:\temp\hub-debug.log', 'a', encoding='utf-8') as _diag2_f:
                _diag2_f.write('=== _fetch_hub_manifest swallow @ ' + __import__('datetime').datetime.now().isoformat() + ' ===\n')
                _diag2_f.write(_diag2_tb.format_exc())
                _diag2_f.write('\n')
        except Exception:
            pass
        return None
    if not isinstance(data, dict):
        return None
    for field in _HUB_MANIFEST_REQUIRED:
        if field not in data or not isinstance(data[field], str):
            _logger.warning("Hub manifest missing or bad field: %s", field)
            return None
    mv = data["manifest_version"]
    if not mv.startswith("1."):
        _logger.warning("Unknown hub manifest major version: %s", mv)
        return None
    sha = data.get("setup_sha256", "")
    if not sha or sha == "PLACEHOLDER_UNTIL_BUILD":
        _logger.info("Hub manifest has placeholder SHA256 — no update available yet")
        return None
    return data


def _download_hub_setup(
    url: str, dest: Path, expected_sha256: str, timeout: int = 120
) -> Optional[Path]:
    """Download + sha256-verify the Hub setup exe. Returns dest on success, None on failure."""
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            if _sha256_file(dest).lower() == expected_sha256.lower():
                return dest
            dest.unlink()
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        actual = _sha256_file(dest)
        if actual.lower() != expected_sha256.lower():
            _logger.warning("Hub setup sha256 mismatch: expected=%s got=%s",
                            expected_sha256, actual)
            try:
                dest.unlink()
            except OSError:
                pass
            return None
        return dest
    except Exception as exc:
        _logger.debug("Hub setup download failed: %s", exc)
        try:
            if dest.exists():
                dest.unlink()
        except OSError:
            pass
        return None


def _compare_versions(a: str, b: str) -> int:
    """Return -1 if a<b, 0 if equal, +1 if a>b. Compares numeric tuples."""
    def tup(s):
        s = (s or "").lstrip("v").split("-", 1)[0].split("+", 1)[0]
        parts = s.split(".") if s else []
        out = []
        for p in parts:
            try:
                out.append(int(p))
            except ValueError:
                out.append(0)
        return tuple(out)
    ta, tb = tup(a), tup(b)
    if ta < tb: return -1
    if ta > tb: return 1
    return 0


def _attempt_hub_update_check(args, window=None) -> None:
    """Launch-time Hub self-update check.

    Contract: returns None on EVERY failure mode.  Hub launch must NEVER fail
    because this check failed.  Only calls sys.exit(0) on the success path.
    """
    try:
        _attempt_hub_update_check_inner(args, window)
    except Exception as exc:
        import traceback as _diag2_tb
        import os as _diag2_os
        try:
            _diag2_os.makedirs(r'C:\temp', exist_ok=True)
            with open(r'C:\temp\hub-debug.log', 'a', encoding='utf-8') as _diag2_f:
                _diag2_f.write('=== _attempt_hub_update_check swallow @ ' + __import__('datetime').datetime.now().isoformat() + ' ===\n')
                _diag2_f.write(_diag2_tb.format_exc())
                _diag2_f.write('\n')
        except Exception as _diag2_write_err:
            _logger.debug("Hub update diag write failed: %s", _diag2_write_err)
        _logger.debug("Hub update check failed (%s); continuing", exc)


def _attempt_hub_update_check_inner(args, window=None) -> None:
    # 1. Flag guard
    if getattr(args, "no_update_check", False):
        return

    # 2. Read hub config
    config = read_hub_config()

    # Phase 4 fix: clear stale pending state if local version already matches
    pending_ver = config.get("update_pending_version")
    if pending_ver and isinstance(pending_ver, str):
        if _compare_versions(WINDOWS_VERSION, pending_ver) >= 0:
            config["update_pending"] = False
            config["update_pending_version"] = None
            write_hub_config(config)

    # 3. Handle CLI config-persist flags
    if getattr(args, "auto_update_hub", False):
        config["auto_update_hub"] = True
        write_hub_config(config)
    elif getattr(args, "no_auto_update_hub", False):
        config["auto_update_hub"] = False
        write_hub_config(config)

    # 4. Fetch manifest
    manifest = _fetch_hub_manifest(timeout=5)
    if manifest is None:
        return

    # 5. Compare versions
    cmp = _compare_versions(manifest["hub_version"], WINDOWS_VERSION)
    if cmp <= 0:
        return  # no update available

    new_ver = manifest["hub_version"]
    cur_ver = WINDOWS_VERSION
    force_update = False

    # 6. Check min_required (optional field)
    min_req = manifest.get("min_required")
    if min_req and isinstance(min_req, str):
        if _compare_versions(cur_ver, min_req) < 0:
            force_update = True

    # Build download dest path
    dest = (
        Path(tempfile.gettempdir())
        / "FryNetworks"
        / "hub-update"
        / f"FryHubSetup-{new_ver}.exe"
    )

    # 7. Auto-update path (silent, no modal)
    if config.get("auto_update_hub") and not force_update:
        _threaded_download_and_launch(manifest, dest, config, window)
        return

    # 8. Show modal (needs QApplication to already exist)
    try:
        from PySide6 import QtWidgets

        dlg = QtWidgets.QMessageBox(parent=window)
        dlg.setWindowIcon(QtWidgets.QApplication.instance().windowIcon())

        is_pending = (
            config.get("update_pending")
            and config.get("update_pending_version") == new_ver
        )

        if is_pending:
            dlg.setWindowTitle("Fry Hub Update Incomplete")
            dlg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            dlg.setText("A previous update did not finish installing.")
            dlg.setInformativeText(
                f"Current: v{cur_ver}\nPending: v{new_ver}\n\n"
                "Retry installation now?"
            )
        elif force_update:
            dlg.setWindowTitle("Fry Hub Update Required")
            dlg.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            dlg.setText("A required update must be installed before continuing.")
            dlg.setInformativeText(
                f"Current: v{cur_ver}\nRequired: v{new_ver}"
            )
        else:
            dlg.setWindowTitle("Fry Hub Update Available")
            dlg.setIcon(QtWidgets.QMessageBox.Icon.Information)
            dlg.setText("A new version of Fry Hub is available.")
            dlg.setInformativeText(
                f"Current: v{cur_ver}\nAvailable: v{new_ver}\n\n"
                "Download and install now?"
            )

        update_btn = dlg.addButton(
            "Update Now", QtWidgets.QMessageBox.ButtonRole.AcceptRole
        )
        if force_update:
            exit_btn = dlg.addButton(
                "Exit", QtWidgets.QMessageBox.ButtonRole.RejectRole
            )
            auto_cb = None
        else:
            skip_btn = dlg.addButton(
                "Skip", QtWidgets.QMessageBox.ButtonRole.RejectRole
            )
            auto_cb = QtWidgets.QCheckBox(
                "Always update automatically (skip this prompt)"
            )
            dlg.setCheckBox(auto_cb)

        dlg.setDefaultButton(update_btn)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        dlg.exec()
        clicked = dlg.clickedButton()

        if clicked == update_btn:
            enable_auto = (
                auto_cb is not None and auto_cb.isChecked()
            )
            _threaded_download_and_launch(
                manifest, dest, config, window, enable_auto_update=enable_auto
            )
            return
        else:
            config["last_update_check_at"] = datetime.now(timezone.utc).isoformat()
            config["last_seen_hub_version"] = new_ver
            if force_update:
                config["update_pending"] = True
                config["update_pending_version"] = new_ver
            write_hub_config(config)
            if force_update:
                sys.exit(0)
            return

    except Exception as exc:
        _logger.warning("Hub update modal failed (%s); skipping update", exc)
        return


def _launch_hub_setup_and_exit(setup_exe: Path, config: dict, manifest: dict, window=None) -> None:
    """Launch Inno Setup via a PowerShell wrapper that waits for Hub to exit."""
    import textwrap as _textwrap

    inno_log = (
        Path(tempfile.gettempdir())
        / "FryNetworks"
        / "hub-update"
        / "fryhub-update-install.log"
    )
    wrapper_dir = inno_log.parent
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    wrapper_path = wrapper_dir / "fryhub-launch-update.ps1"

    ps_script = _textwrap.dedent(r"""\
    param(
        [int]$HubPid,
        [int]$BootloaderPid,
        [string]$InnoExe,
        [string]$InnoLog
    )

    $wrapperLog = "$env:TEMP\FryNetworks\hub-update\wrapper-diag-$(Get-Date -Format 'yyyyMMddHHmmss').log"
    function W($msg) { "$([DateTime]::UtcNow.ToString('o')) $msg" | Out-File -FilePath $wrapperLog -Append -Encoding UTF8 }
    W "WRAPPER_START HubPid=$HubPid InnoExe=$InnoExe InnoLog=$InnoLog"
    W "PSCommandPath=$PSCommandPath"
    W "Identity: $([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)"
    W "Elevated: $(([System.Security.Principal.WindowsPrincipal][System.Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator))"
    W "InnoExe Test-Path: $(Test-Path $InnoExe)"
    if (Test-Path $InnoExe) { W "InnoExe size: $((Get-Item $InnoExe).Length)" }
    W "InnoLog parent Test-Path: $(Test-Path (Split-Path $InnoLog -Parent))"
    $preSha = (Get-FileHash -Algorithm SHA256 -Path 'C:\Program Files\FryNetworks\frynetworks_installer.exe' -ErrorAction SilentlyContinue).Hash
    W "Pre-Inno target SHA: $preSha"

    $elapsed = 0
    $hubAlive = $true
    $blAlive = $true
    while ($elapsed -lt 60) {
        $hubAlive = (Get-Process -Id $HubPid -ErrorAction SilentlyContinue) -ne $null
        $blAlive = (Get-Process -Id $BootloaderPid -ErrorAction SilentlyContinue) -ne $null
        if ((-not $hubAlive) -and (-not $blAlive)) {
            W "Post-poll: both PIDs exited after ${elapsed}s"
            break
        }
        Start-Sleep -Seconds 1
        $elapsed++
    }
    if ($hubAlive -or $blAlive) {
        W "Post-poll: TIMEOUT after 60s HubAlive=$hubAlive BootloaderAlive=$blAlive"
        exit 62
    }
    W "Pre-Inno launch"

    try {
        $innoProc = Start-Process -FilePath $InnoExe -ArgumentList @('/SILENT','/SP-','/SUPPRESSMSGBOXES','/CLOSEAPPLICATIONS','/RESTARTAPPLICATIONS','/NORESTART',('/LOG=' + $InnoLog)) -WindowStyle Hidden -PassThru -Wait
        W "Inno exit: code=$($innoProc.ExitCode) pid=$($innoProc.Id) hasExited=$($innoProc.HasExited)"
    } catch {
        W "Inno launch EXCEPTION: $($_ | Out-String)"
    }

    $postSha = (Get-FileHash -Algorithm SHA256 -Path 'C:\Program Files\FryNetworks\frynetworks_installer.exe' -ErrorAction SilentlyContinue).Hash
    W "Post-Inno target SHA: $postSha"
    W "WRAPPER_END"

    try { Move-Item -LiteralPath $PSCommandPath -Destination "$PSCommandPath.completed-$(Get-Date -Format 'yyyyMMddHHmmss')" -Force -ErrorAction SilentlyContinue } catch {}
    """)
    wrapper_path.write_text(ps_script, encoding="utf-8")

    try:
        config["last_update_check_at"] = datetime.now(timezone.utc).isoformat()
        config["last_seen_hub_version"] = manifest["hub_version"]
        write_hub_config(config)

        p = subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass",
                "-WindowStyle", "Hidden",
                "-File", str(wrapper_path),
                "-HubPid", str(os.getpid()),
                "-BootloaderPid", str(os.getppid()),
                "-InnoExe", str(setup_exe),
                "-InnoLog", str(inno_log),
            ],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        _logger.info("Hub-update wrapper Popen returned PID=%d", p.pid)

        try:
            p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        else:
            exit_code = p.poll()
            _logger.error("Update wrapper exited early with code %s", exit_code)
            if window is not None:
                from PySide6 import QtWidgets
                QtWidgets.QMessageBox.critical(
                    window,
                    "Update Failed",
                    "The update installer could not be started. "
                    "Please try again later or download the latest version from fry.farm.",
                )
            return

        sys.exit(0)
    except Exception as exc:
        _logger.error("Failed to launch update wrapper: %s", exc)
        if window is not None:
            from PySide6 import QtWidgets
            QtWidgets.QMessageBox.critical(
                window,
                "Update Failed",
                "The update installer could not be started. "
                "Please try again later or download the latest version from fry.farm.",
            )


def _threaded_download_and_launch(
    manifest: dict,
    dest: Path,
    config: dict,
    window,
    enable_auto_update: bool = False,
) -> None:
    """Offload _download_hub_setup to a thread and marshal result back to main thread."""
    def _download_worker() -> None:
        try:
            setup_path = _download_hub_setup(
                manifest["setup_url"], dest, manifest["setup_sha256"]
            )
        except Exception as exc:
            _logger.warning("Hub update download failed: %s", exc)
            setup_path = None
        try:
            from PySide6 import QtCore
            QtCore.QTimer.singleShot(
                0,
                lambda: _on_download_done(
                    setup_path, manifest, config, window, enable_auto_update
                ),
            )
        except Exception as exc:
            _logger.warning("Failed to marshal hub update result: %s", exc)

    def _on_download_done(
        setup_path: Optional[Path],
        manifest: dict,
        config: dict,
        window,
        enable_auto_update: bool,
    ) -> None:
        if setup_path is None:
            _logger.debug("Hub update download failed; continuing launch")
            return
        if enable_auto_update:
            config["auto_update_hub"] = True
        config["last_update_check_at"] = datetime.now(timezone.utc).isoformat()
        config["last_seen_hub_version"] = manifest["hub_version"]
        config["update_pending"] = True
        config["update_pending_version"] = manifest["hub_version"]
        write_hub_config(config)
        _launch_hub_setup_and_exit(setup_path, config, manifest, window)

    threading.Thread(target=_download_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# PoC binary update functions (ported from tools/updater.py)
# ---------------------------------------------------------------------------

_DEFAULT_POC_REPO = "Fry-Foundation/HardwarePoC_releases"
_DEFAULT_POC_CONFIG_DIR = Path(r"C:\ProgramData\FryNetworks")
_POC_LOG_PATH = Path(r"C:\ProgramData\FryNetworks\updater\fryhub_updater.log")


def _poc_log(msg: str, log_path: Path = _POC_LOG_PATH) -> None:
    """Append timestamped line to updater log file."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _fetch_json(url: str, token: Optional[str] = None) -> dict:
    """GET a JSON endpoint. Optional Bearer token."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_file(url: str, dest: Path, token: Optional[str] = None) -> None:
    """Stream-download a binary file with optional Bearer token."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def _backfill_poc_configs(config_dir: Path) -> None:
    """Ensure miner_code and poc_version exist in installer_config.json files.

    Lightweight port of tools/config_backfill.backfill_poc_discovery_fields().
    Silently skips any dirs that already have both fields.
    """
    import re as _re
    for miner_dir in sorted(config_dir.iterdir()):
        if not miner_dir.is_dir() or not miner_dir.name.startswith("miner-"):
            continue
        code_part = miner_dir.name[len("miner-"):]
        if not code_part or "." in code_part:
            continue
        cfg_path = miner_dir / "config" / "installer_config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        changed = False
        if "miner_code" not in cfg:
            cfg["miner_code"] = code_part
            changed = True
        if "poc_version" not in cfg:
            for exe in miner_dir.glob(f"FRY_PoC_{code_part}_v*.exe"):
                if ".bak" in exe.name:
                    continue
                m = _re.search(r"_v(.+)\.exe$", exe.name)
                if m:
                    cfg["poc_version"] = m.group(1)
                    changed = True
                    break
        if changed:
            try:
                tmp = cfg_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
                os.replace(str(tmp), str(cfg_path))
            except Exception:
                pass


def _discover_poc_installs(config_dir: Path) -> list:
    """Scan for installed PoC miners. Returns list of install info dicts."""
    installs = []
    for cfg in sorted(config_dir.glob("miner-*/config/installer_config.json")):
        miner_name = cfg.parent.parent.name
        if "." in miner_name[len("miner-"):]:
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        miner_code = data.get("miner_code")
        poc_version = data.get("poc_version")
        if not miner_code or not poc_version:
            continue
        install_root = cfg.parent.parent
        nssm_path = install_root / "nssm.exe"
        if not nssm_path.exists():
            continue
        installs.append({
            "miner_code": miner_code,
            "poc_version": poc_version,
            "install_root": install_root,
            "nssm_path": nssm_path,
            "config_path": cfg,
        })
    return installs


def _find_poc_asset(release: dict, miner_code: str) -> Optional[dict]:
    """Find a PoC service asset for *miner_code* in a GitHub release."""
    prefix = f"FRY_PoC_{miner_code}_v".lower()
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not name.lower().startswith(prefix) or not name.lower().endswith(".exe"):
            continue
        ver_part = name.rsplit("_v", 1)[-1].rsplit(".", 1)[0]
        if not ver_part:
            continue
        sha_name = (name + ".sha256").lower()
        sha_asset = next(
            (a for a in release.get("assets", [])
             if a.get("name", "").lower() == sha_name),
            None,
        )
        return {
            "name": name,
            "url": asset.get("browser_download_url"),
            "version": ver_part,
            "sha_asset": sha_asset,
        }
    return None


def _update_poc_service(
    info: dict,
    new_exe_dest: Path,
    new_version: str,
    log_path: Path = _POC_LOG_PATH,
) -> None:
    """Stop -> backup -> swap -> re-register -> start a PoC service via nssm."""
    import shutil
    import time as _time

    miner_code = info["miner_code"]
    install_root = info["install_root"]
    nssm = str(info["nssm_path"])
    old_service = f"FRY_PoC_{miner_code}_v{info['poc_version']}"
    new_service = f"FRY_PoC_{miner_code}_v{new_version}"
    new_exe_path = install_root / f"FRY_PoC_{miner_code}_v{new_version}.exe"

    # 1. Stop old service
    result = subprocess.run(
        [nssm, "stop", old_service],
        check=False, capture_output=True, timeout=30,
    )
    _time.sleep(2)
    status = subprocess.run(
        [nssm, "status", old_service],
        capture_output=True, timeout=10,
    )
    svc_state = status.stdout.decode("utf-16-le", errors="ignore").strip()
    if "STOPPED" not in svc_state and "SERVICE_STOPPED" not in svc_state:
        raise RuntimeError(
            f"Service {old_service} failed to stop "
            f"(nssm stop rc={result.returncode}, status={svc_state!r})"
        )
    _poc_log(f"[INFO] STOPPED: {old_service}", log_path)

    # 2. Remove old service
    subprocess.run(
        [nssm, "remove", old_service, "confirm"],
        check=True, capture_output=True, timeout=15,
    )
    _poc_log(f"[INFO] REMOVED: {old_service}", log_path)

    # 3. Backup old exe(s)
    ts = int(_time.time())
    for old_exe in install_root.glob(f"FRY_PoC_{miner_code}_v*.exe"):
        if ".bak" in old_exe.name:
            continue
        bak = old_exe.parent / f"{old_exe.name}.bak.{ts}"
        shutil.copy2(str(old_exe), str(bak))
        _poc_log(f"[INFO] BACKUP: {bak}", log_path)

    # 4. Install new exe
    shutil.copy2(str(new_exe_dest), str(new_exe_path))
    _poc_log(f"[INFO] INSTALLED: {new_exe_path}", log_path)

    # 5. Register new service
    logs_dir = install_root / "logs"
    logs_dir.mkdir(exist_ok=True)
    subprocess.run(
        [nssm, "install", new_service, str(new_exe_path)],
        check=True, capture_output=True, timeout=30,
    )
    for key, val in [
        ("AppDirectory", str(install_root)),
        ("AppStdout", str(logs_dir / "service.out.log")),
        ("AppStderr", str(logs_dir / "service.err.log")),
        ("AppRotateFiles", "1"),
        ("AppRotateBytes", "1048576"),
        ("Start", "SERVICE_AUTO_START"),
    ]:
        subprocess.run(
            [nssm, "set", new_service, key, val],
            check=True, capture_output=True, timeout=10,
        )
    _poc_log(f"[INFO] REGISTERED: {new_service}", log_path)

    # 6. Start new service
    subprocess.run(
        [nssm, "start", new_service],
        check=True, capture_output=True, timeout=30,
    )
    _poc_log(f"[INFO] STARTED: {new_service}", log_path)

    # 7. Update installer_config.json
    cfg_path = info["config_path"]
    try:
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_data["poc_version"] = new_version
        tmp = cfg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp), str(cfg_path))
        _poc_log(f"[INFO] CONFIG UPDATED: {cfg_path}", log_path)
    except Exception as exc:
        _poc_log(f"[WARN] config update failed ({cfg_path}): {exc}", log_path)


def update_installed_poc_binaries(
    config_dir: Path = _DEFAULT_POC_CONFIG_DIR,
    poc_repo: str = _DEFAULT_POC_REPO,
    token: Optional[str] = None,
    dry_run: bool = False,
    log_path: Path = _POC_LOG_PATH,
) -> Dict[str, Any]:
    """Check and update all installed PoC service binaries.

    Returns dict: {"updated": [...], "skipped": [...], "errors": [...]}
    """
    result: Dict[str, Any] = {"updated": [], "skipped": [], "errors": []}

    _backfill_poc_configs(config_dir)
    installs = _discover_poc_installs(config_dir)
    if not installs:
        _poc_log("[INFO] No PoC installations found.", log_path)
        return result

    github_token = token or os.environ.get("GITHUB_TOKEN") or ""

    for inst in installs:
        miner_code = inst["miner_code"]
        try:
            release = _fetch_json(
                f"https://api.github.com/repos/{poc_repo}/releases/latest",
                github_token or None,
            )
        except Exception as exc:
            msg = f"PoC release check failed for {miner_code}: {exc}"
            _poc_log(f"[ERROR] {msg}", log_path)
            result["errors"].append(msg)
            continue

        asset = _find_poc_asset(release, miner_code)
        if not asset:
            _poc_log(f"[INFO] No PoC asset for {miner_code} in latest release.", log_path)
            result["skipped"].append(miner_code)
            continue

        installed_ver = inst["poc_version"]
        remote_ver = asset["version"]
        if _compare_versions(remote_ver, installed_ver) <= 0:
            _poc_log(f"[INFO] PoC up to date for {miner_code}: v{installed_ver}", log_path)
            result["skipped"].append(miner_code)
            continue

        _poc_log(
            f"[INFO] PoC update available for {miner_code}: "
            f"v{installed_ver} -> v{remote_ver}",
            log_path,
        )

        if dry_run:
            _poc_log(f"[DRY-RUN] Would update PoC for {miner_code}", log_path)
            result["skipped"].append(miner_code)
            continue

        asset_url = asset["url"]
        if not asset_url:
            msg = f"PoC asset for {miner_code} missing download URL."
            _poc_log(f"[ERROR] {msg}", log_path)
            result["errors"].append(msg)
            continue

        dest = Path(tempfile.gettempdir()) / asset["name"]
        _poc_log(f"[INFO] Downloading {asset_url} to {dest}", log_path)
        try:
            _download_file(asset_url, dest, github_token or None)
        except Exception as exc:
            msg = f"PoC download failed for {miner_code}: {exc}"
            _poc_log(f"[ERROR] {msg}", log_path)
            result["errors"].append(msg)
            continue

        # SHA256 verification from companion .sha256 asset
        sha_asset = asset.get("sha_asset")
        if sha_asset:
            sha_url = sha_asset.get("browser_download_url")
            if sha_url:
                sha_dest = dest.with_suffix(dest.suffix + ".sha256")
                try:
                    _download_file(sha_url, sha_dest, github_token or None)
                    expected = sha_dest.read_text().split()[0].strip()
                    actual = _sha256_file(dest)
                    if expected.lower() != actual.lower():
                        msg = (
                            f"PoC checksum mismatch for {miner_code}: "
                            f"expected {expected}, got {actual}"
                        )
                        _poc_log(f"[ERROR] {msg}", log_path)
                        result["errors"].append(msg)
                        continue
                    _poc_log(f"[INFO] PoC checksum verified for {miner_code}.", log_path)
                except Exception as exc:
                    _poc_log(f"[WARN] SHA256 check failed for {miner_code}: {exc}", log_path)

        try:
            _update_poc_service(inst, dest, asset["version"], log_path)
            result["updated"].append(miner_code)
        except Exception as exc:
            msg = (
                f"PoC update FAILED for {miner_code} "
                f"(service may be in partial state): {exc}"
            )
            _poc_log(f"[ERROR] {msg}", log_path)
            result["errors"].append(msg)

    return result


# ---------------------------------------------------------------------------
# Headless update mode (for scheduled task / CLI invocation)
# ---------------------------------------------------------------------------

def run_headless_updates(args) -> int:
    """Run update checks without GUI. Returns exit code 0-7.

    Exit codes:
      0 = no updates needed
      1 = hub updated (relaunch pending)
      2 = PoC binaries updated
      3 = both hub and PoC updated
      4 = check failed (network / manifest)
      5 = download failed
      6 = apply failed
      7 = config error
    """
    log_path = _POC_LOG_PATH
    _poc_log("[INFO] === Headless update check started ===", log_path)

    # 10-minute timeout via threading.Timer (signal.alarm not available on Windows)
    timeout_event = threading.Event()
    timer = threading.Timer(600, lambda: timeout_event.set())
    timer.daemon = True
    timer.start()

    hub_updated = False
    poc_updated = False
    had_error = False

    update_poc = getattr(args, "update_poc", False) or getattr(args, "update_all", False)
    update_hub = not getattr(args, "update_poc", False) or getattr(args, "update_all", False)

    try:
        # Hub self-update check
        if update_hub and not timeout_event.is_set():
            _poc_log("[INFO] Checking for Hub updates...", log_path)
            manifest = _fetch_hub_manifest(timeout=10)
            if manifest is None:
                _poc_log("[INFO] No Hub update available (or manifest unreachable).", log_path)
            else:
                cmp = _compare_versions(manifest["hub_version"], WINDOWS_VERSION)
                if cmp > 0:
                    _poc_log(
                        f"[INFO] Hub update available: v{WINDOWS_VERSION} -> "
                        f"v{manifest['hub_version']}",
                        log_path,
                    )
                    dest = (
                        Path(tempfile.gettempdir())
                        / "FryNetworks"
                        / "hub-update"
                        / f"FryHubSetup-{manifest['hub_version']}.exe"
                    )
                    setup = _download_hub_setup(
                        manifest["setup_url"], dest,
                        manifest["setup_sha256"], timeout=120,
                    )
                    if setup is not None:
                        hub_updated = True
                        _poc_log(f"[INFO] Hub setup downloaded: {setup}", log_path)
                        # Launch silent installer and exit
                        config = read_hub_config()
                        config["update_pending"] = True
                        config["update_pending_version"] = manifest["hub_version"]
                        write_hub_config(config)
                        _launch_hub_setup_and_exit(setup, config, manifest, window=None)
                    else:
                        had_error = True
                        _poc_log("[ERROR] Hub setup download failed.", log_path)
                else:
                    _poc_log(f"[INFO] Hub already at latest: v{WINDOWS_VERSION}", log_path)

        # PoC binary updates
        if update_poc and not timeout_event.is_set():
            _poc_log("[INFO] Checking for PoC updates...", log_path)
            poc_result = update_installed_poc_binaries(log_path=log_path)
            if poc_result["updated"]:
                poc_updated = True
                _poc_log(
                    f"[INFO] PoC updated: {', '.join(poc_result['updated'])}",
                    log_path,
                )
            if poc_result["errors"]:
                had_error = True

        if timeout_event.is_set():
            _poc_log("[ERROR] Headless update timed out (10 min).", log_path)
            return 4

    except SystemExit:
        # _launch_hub_setup_and_exit calls sys.exit(0) — let it propagate
        raise
    except Exception as exc:
        _poc_log(f"[ERROR] Headless update exception: {exc}", log_path)
        return 4
    finally:
        timer.cancel()
        _poc_log("[INFO] === Headless update check finished ===", log_path)

    if had_error:
        return 5 if not (hub_updated or poc_updated) else 6
    if hub_updated and poc_updated:
        return 3
    if hub_updated:
        return 1
    if poc_updated:
        return 2
    return 0
