"""
FryNetworks Installer Updater

Checks the Bunny CDN manifest for a newer installer, downloads it, verifies
SHA256, and launches the new installer. Also handles PoC service binary
updates via GitHub Releases. Designed to run silently from a scheduled task.

Exit codes:
    0 — Success or no update needed
    2 — Manifest fetch failed
    3 — Manifest missing required fields
    4 — Download failed (partial file cleaned up)
    5 — SHA256 mismatch (downloaded file deleted)
    6 — Installer execution failed (services may be stopped)
    7 — Version discovery failed
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, cast


# --- Constants ---

DEFAULT_MANIFEST_URL = (
    "https://frynetworks-downloads.b-cdn.net/frynetworks-installer/latest/version.json"
)
DEFAULT_LOG_PATH = Path(r"C:\ProgramData\FryNetworks\updater\updater.log")
DEFAULT_TASK_NAME = "FryNetworksUpdater"
DEFAULT_EMBEDDED_TOKEN = os.getenv("EMBEDDED_GITHUB_TOKEN", "")
DEFAULT_POC_REPO = "Fry-Foundation/HardwarePoC_releases"
DEFAULT_POC_CONFIG_DIR = r"C:\ProgramData\FryNetworks"

# Kept for backward compat (PoC updates still use GitHub)
DEFAULT_REPO = "Fry-Foundation/HardwareInstaller-Public"

MANIFEST_REQUIRED_FIELDS = ("version", "sha256", "download_url")


# --- Logging ---

def write_log(msg: str, dest: Path) -> None:
    """Append a timestamped log line to the updater log file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}\n"
    with open(dest, "a", encoding="utf-8") as f:
        f.write(line)


def log_and_print(msg: str, log_file: Path, quiet: bool = False) -> None:
    """Write to log file always; print to stdout unless --quiet."""
    write_log(msg, log_file)
    if not quiet:
        print(msg)


# --- Version utilities ---

def normalize_version(ver: str) -> str:
    ver = ver.strip()
    return ver if ver.startswith("v") else f"v{ver}"


def compare_versions(a: str, b: str) -> int:
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
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


# --- Version discovery ---

def _pe_file_version(exe_path: Path) -> Optional[str]:
    """Read PE FileVersion resource via PowerShell. Returns version string or None."""
    if not exe_path.exists():
        return None
    try:
        result = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                f"[System.Diagnostics.FileVersionInfo]::GetVersionInfo('{exe_path}').FileVersion"
            ],
            capture_output=True, timeout=10, text=True,
        )
        ver = result.stdout.strip()
        if ver and ver != "":
            return ver
    except Exception:
        pass
    return None


def _find_installer_exe(base_dir: Path) -> Optional[Path]:
    """Find frynetworks_installer*.exe under miner-* dirs."""
    for miner_dir in sorted(base_dir.glob("miner-*")):
        if not miner_dir.is_dir() or "." in miner_dir.name[len("miner-"):]:
            continue
        for exe in miner_dir.glob("frynetworks_installer*.exe"):
            if exe.is_file():
                return exe
    return None


def _read_config_versions(base_dir: Path) -> list[tuple[str, Path]]:
    """Read installer_version from all miner-*/config/installer_config.json.
    Returns list of (version, path) sorted by version descending."""
    versions = []
    for cfg in sorted(base_dir.glob("miner-*/config/installer_config.json")):
        # Skip backup dirs
        miner_name = cfg.parent.parent.name
        if "." in miner_name[len("miner-"):]:
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            ver = data.get("installer_version")
            if ver and ver.strip():
                versions.append((ver.strip(), cfg))
        except Exception:
            continue
    # Sort by version descending
    versions.sort(key=lambda x: x[0], reverse=True)
    return versions


def discover_installer_version(
    cli_version: Optional[str],
    poc_config_dir: str,
    log_file: Path,
) -> Optional[str]:
    """Discover the installed version via cascade:
      1. CLI --current-version argument
      2. PE FileVersion of frynetworks_installer*.exe under miner dirs
      3. installer_config.json → MAX installer_version across all miner dirs
      4. None (caller should exit 7)
    """
    base_dir = Path(poc_config_dir)

    # 1. CLI argument
    if cli_version:
        ver = normalize_version(cli_version)
        write_log(f"[INFO] Version source: CLI argument -> {ver}", log_file)
        return ver

    # 2. PE FileVersion of installer EXE
    installer_exe = _find_installer_exe(base_dir)
    if installer_exe:
        pe_ver = _pe_file_version(installer_exe)
        if pe_ver:
            ver = normalize_version(pe_ver)
            write_log(f"[INFO] Version source: PE FileVersion of {installer_exe} -> {ver}", log_file)
            return ver
        else:
            write_log(f"[INFO] Installer EXE found at {installer_exe} but no PE FileVersion", log_file)

    # 3. Config file fallback — pick MAX across all miner dirs
    config_versions = _read_config_versions(base_dir)
    if config_versions:
        all_vers = [(v, str(p)) for v, p in config_versions]
        write_log(f"[INFO] Config versions found: {all_vers}", log_file)
        # Pick the maximum version
        best_ver = config_versions[0][0]
        for v, _ in config_versions[1:]:
            if compare_versions(v, best_ver) > 0:
                best_ver = v
        ver = normalize_version(best_ver)
        write_log(f"[INFO] Version source: MAX config installer_version -> {ver}", log_file)
        return ver

    # 4. Failed
    write_log("[ERROR] Version discovery failed: no CLI arg, no installer EXE, no usable config", log_file)
    return None


# --- Network utilities ---

def fetch_json(url: str, token: Optional[str] = None) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_manifest(url: str, log_file: Path) -> Optional[dict]:
    """Fetch and validate the Bunny CDN installer manifest.

    Returns manifest dict on success, None on failure (logged).
    """
    try:
        manifest = fetch_json(url)
    except Exception as e:
        write_log(f"[ERROR] Manifest fetch failed from {url}: {e}", log_file)
        return None

    # Validate required fields
    missing = [f for f in MANIFEST_REQUIRED_FIELDS if f not in manifest or not manifest[f]]
    if missing:
        write_log(
            f"[ERROR] Manifest at {url} missing required fields: {missing}",
            log_file,
        )
        return None

    return manifest


def download(url: str, dest: Path, token: Optional[str] = None) -> None:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req, timeout=300) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# --- Installer execution ---

def run_installer(installer_path: Path, quiet: bool, log_file: Path) -> int:
    """Run installer BLOCKING, capturing output to log. Returns exit code."""
    args = [str(installer_path)]
    if quiet:
        args.append("--quiet")
        args.append("--no-update-check")
    write_log(f"[INFO] Update in progress — services will be down during install window.", log_file)
    write_log(f"[INFO] Running installer: {' '.join(args)}", log_file)

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            timeout=600,  # 10 min max
            text=True,
        )
        if result.stdout:
            for line in result.stdout.splitlines():
                write_log(f"[INSTALLER-STDOUT] {line}", log_file)
        if result.stderr:
            for line in result.stderr.splitlines():
                write_log(f"[INSTALLER-STDERR] {line}", log_file)
        write_log(f"[INFO] Installer exited with code {result.returncode}", log_file)
        return result.returncode
    except subprocess.TimeoutExpired:
        write_log("[ERROR] Installer timed out after 600s", log_file)
        return -1
    except Exception as e:
        write_log(f"[ERROR] Installer execution failed: {e}", log_file)
        return -1


# --- PoC update functions (unchanged — still use GitHub Releases) ---

def find_asset(release: dict, suffix: str) -> Optional[dict]:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(suffix.lower()):
            return asset
    return None


def find_poc_asset(release: dict, miner_code: str) -> Optional[dict]:
    """Find a PoC service asset for *miner_code* in *release*."""
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


def discover_poc_installs(config_dir: Path) -> list:
    """Scan *config_dir* for installed PoC miners."""
    installs = []
    for cfg in sorted(config_dir.glob("miner-*/config/installer_config.json")):
        # Skip backup dirs
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
        install_root = cfg.parent.parent  # miner-{CODE}
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


def update_poc_service(
    info: dict,
    new_exe_dest: Path,
    new_version: str,
    log_file: Path,
) -> None:
    """Stop -> backup -> swap -> re-register -> start a PoC service."""
    miner_code = info["miner_code"]
    install_root = info["install_root"]
    nssm = str(info["nssm_path"])
    old_service = f"FRY_PoC_{miner_code}_v{info['poc_version']}"
    new_service = f"FRY_PoC_{miner_code}_v{new_version}"
    new_exe_path = install_root / f"FRY_PoC_{miner_code}_v{new_version}.exe"

    # 1. Stop
    result = subprocess.run(
        [nssm, "stop", old_service],
        check=False, capture_output=True, timeout=30,
    )
    time.sleep(2)
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
    write_log(f"[INFO] STOPPED: {old_service}", log_file)

    # 2. Remove
    subprocess.run(
        [nssm, "remove", old_service, "confirm"],
        check=True, capture_output=True, timeout=15,
    )
    write_log(f"[INFO] REMOVED: {old_service}", log_file)

    # 3. Backup old exe(s)
    ts = int(time.time())
    for old_exe in install_root.glob(f"FRY_PoC_{miner_code}_v*.exe"):
        if ".bak" in old_exe.name:
            continue
        bak = old_exe.parent / f"{old_exe.name}.bak.{ts}"
        shutil.copy2(str(old_exe), str(bak))
        write_log(f"[INFO] BACKUP: {bak}", log_file)

    # 4. Install new exe
    shutil.copy2(str(new_exe_dest), str(new_exe_path))
    write_log(f"[INFO] INSTALLED: {new_exe_path}", log_file)

    # 5. Register
    logs_dir = install_root / "logs"
    logs_dir.mkdir(exist_ok=True)

    subprocess.run(
        [nssm, "install", new_service, str(new_exe_path)],
        check=True, capture_output=True, timeout=30,
    )
    subprocess.run(
        [nssm, "set", new_service, "AppDirectory", str(install_root)],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        [nssm, "set", new_service, "AppStdout", str(logs_dir / "service.out.log")],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        [nssm, "set", new_service, "AppStderr", str(logs_dir / "service.err.log")],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        [nssm, "set", new_service, "AppRotateFiles", "1"],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        [nssm, "set", new_service, "AppRotateBytes", "1048576"],
        check=True, capture_output=True, timeout=10,
    )
    subprocess.run(
        [nssm, "set", new_service, "Start", "SERVICE_AUTO_START"],
        check=True, capture_output=True, timeout=10,
    )
    write_log(f"[INFO] REGISTERED: {new_service}", log_file)

    # 6. Start
    subprocess.run(
        [nssm, "start", new_service],
        check=True, capture_output=True, timeout=30,
    )
    write_log(f"[INFO] STARTED: {new_service}", log_file)

    # 7. Update installer_config.json
    cfg_path = info["config_path"]
    try:
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_data["poc_version"] = new_version
        tmp_path = cfg_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")
        os.replace(str(tmp_path), str(cfg_path))
        write_log(f"[INFO] CONFIG UPDATED: {cfg_path}", log_file)
    except Exception as exc:
        write_log(f"[WARN] config update failed ({cfg_path}): {exc}", log_file)


def run_poc_updates(args: argparse.Namespace, log_file: Path) -> int:
    """Check and apply PoC binary updates for all installed miners."""
    token = (
        args.poc_token
        or args.token
        or os.environ.get("GITHUB_TOKEN")
        or DEFAULT_EMBEDDED_TOKEN
        or None
    )
    installs = discover_poc_installs(Path(args.poc_config_dir))
    if not installs:
        write_log("[INFO] No PoC installations found.", log_file)
        return 0

    for inst in installs:
        miner_code = inst["miner_code"]
        try:
            release = fetch_json(
                f"https://api.github.com/repos/{args.poc_repo}/releases/latest",
                token,
            )
        except Exception as exc:
            write_log(f"[ERROR] PoC release check failed for {miner_code}: {exc}", log_file)
            continue

        asset = find_poc_asset(release, miner_code)
        if not asset:
            write_log(f"[INFO] No PoC asset for {miner_code} in latest release.", log_file)
            continue

        installed_ver = normalize_version(inst["poc_version"])
        remote_ver = normalize_version(asset["version"])
        if compare_versions(remote_ver, installed_ver) <= 0:
            write_log(f"[INFO] PoC up to date for {miner_code}: {installed_ver}", log_file)
            continue

        write_log(
            f"[INFO] PoC update available for {miner_code}: {installed_ver} -> {remote_ver}",
            log_file,
        )

        if args.dry_run:
            write_log(f"[DRY-RUN] Would update PoC for {miner_code}", log_file)
            continue

        asset_url = asset["url"]
        if not asset_url:
            write_log(f"[ERROR] PoC asset for {miner_code} missing download URL.", log_file)
            continue
        dest = Path(tempfile.gettempdir()) / asset["name"]
        write_log(f"[INFO] Downloading {asset_url} to {dest}", log_file)
        download(cast(str, asset_url), dest, token)

        # SHA256 verification
        sha_asset = asset.get("sha_asset")
        if sha_asset:
            sha_url = sha_asset.get("browser_download_url")
            if sha_url:
                sha_dest = dest.with_suffix(dest.suffix + ".sha256")
                download(cast(str, sha_url), sha_dest, token)
                expected = sha_dest.read_text().split()[0].strip()
                actual = sha256_file(dest)
                if expected.lower() != actual.lower():
                    write_log(
                        f"[ERROR] PoC checksum mismatch for {miner_code}: "
                        f"expected {expected}, got {actual}",
                        log_file,
                    )
                    continue
                write_log(f"[INFO] PoC checksum verified for {miner_code}.", log_file)

        try:
            update_poc_service(inst, dest, asset["version"], log_file)
        except Exception as exc:
            write_log(
                f"[ERROR] PoC update FAILED for {miner_code} "
                f"(service may be in partial state): {exc}",
                log_file,
            )
    return 0


# --- CLI ---

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Update Fry Hub from Bunny CDN manifest."
    )
    p.add_argument(
        "--manifest-url", default=None,
        help="Bunny CDN manifest URL override (default: built-in production URL).",
    )
    p.add_argument(
        "--current-version",
        help="Current version (e.g., v1.0.0). If omitted, auto-discover from PE/config.",
    )
    p.add_argument(
        "--token",
        help="GitHub token for PoC repo (optional, for private repos).",
    )
    p.add_argument("--quiet", action="store_true", help="Suppress stdout output.")
    p.add_argument(
        "--log", type=Path, default=None,
        help=f"Log file path (default: {DEFAULT_LOG_PATH}).",
    )
    p.add_argument("--dry-run", action="store_true", help="Do not download/install, just report.")
    p.add_argument(
        "--update-poc", action="store_true",
        help="Also check/update PoC service binaries.",
    )
    p.add_argument(
        "--poc-repo", default=DEFAULT_POC_REPO,
        help="GitHub repo for PoC releases (default: %(default)s)",
    )
    p.add_argument(
        "--poc-token",
        help="GitHub token for PoC repo (falls back to --token / GITHUB_TOKEN).",
    )
    p.add_argument(
        "--poc-config-dir", default=DEFAULT_POC_CONFIG_DIR,
        help="Parent dir containing miner-* subdirs (default: %(default)s)",
    )
    # Deprecated — kept for backward compat
    p.add_argument("--repo", default=DEFAULT_REPO, help=argparse.SUPPRESS)
    return p.parse_args()


# --- Main ---

def main() -> int:
    args = parse_args()
    log_file = args.log or DEFAULT_LOG_PATH
    quiet = args.quiet

    write_log("=" * 60, log_file)
    write_log("[INFO] FryNetworks Updater started", log_file)

    # --- Backfill PoC discovery fields (idempotent) ---
    try:
        from tools.config_backfill import backfill_poc_discovery_fields
    except ImportError:
        try:
            # When running as frozen exe, module may be at top level
            from config_backfill import backfill_poc_discovery_fields
        except ImportError:
            backfill_poc_discovery_fields = None

    if backfill_poc_discovery_fields:
        try:
            bf_result = backfill_poc_discovery_fields(
                base_dir=args.poc_config_dir,
                dry_run=args.dry_run,
            )
            updated = bf_result.get("updated", []) + bf_result.get("created", [])
            if updated:
                write_log(f"[INFO] Backfill touched {len(updated)} config(s)", log_file)
        except Exception as e:
            write_log(f"[WARN] Backfill failed (non-fatal): {e}", log_file)

    # --- Version discovery ---
    current_version = discover_installer_version(
        args.current_version, args.poc_config_dir, log_file
    )
    if not current_version:
        log_and_print("CANNOT_DETERMINE_INSTALLER_VERSION", log_file, quiet)
        if args.update_poc:
            write_log("[INFO] Version unknown; proceeding with PoC update only.", log_file)
            return run_poc_updates(args, log_file)
        return 7

    log_and_print(f"Current version: {current_version}", log_file, quiet)

    # --- Fetch Bunny CDN manifest ---
    manifest_url = args.manifest_url or DEFAULT_MANIFEST_URL
    write_log(f"[INFO] Fetching manifest from {manifest_url}", log_file)
    manifest = fetch_manifest(manifest_url, log_file)

    if not manifest:
        # fetch_manifest already logged the error
        if args.update_poc:
            poc_rc = run_poc_updates(args, log_file)
            return poc_rc if poc_rc != 0 else 2
        return 2

    remote_ver = normalize_version(manifest["version"])
    write_log(f"[INFO] Latest available: {remote_ver}", log_file)

    # --- Compare versions ---
    cmp = compare_versions(remote_ver, current_version)
    if cmp <= 0:
        log_and_print(f"No update needed (current={current_version}, latest={remote_ver}).", log_file, quiet)
        if args.update_poc:
            return run_poc_updates(args, log_file)
        return 0

    log_and_print(
        f"Update available: {current_version} -> {remote_ver}",
        log_file, quiet,
    )

    if args.dry_run:
        write_log(f"[DRY-RUN] Would download {manifest['download_url']}", log_file)
        log_and_print("[DRY-RUN] Skipping download and install.", log_file, quiet)
        if args.update_poc:
            return run_poc_updates(args, log_file)
        return 0

    # --- Download installer ---
    filename = manifest.get("filename", "frynetworks_installer.exe")
    dest = Path(tempfile.gettempdir()) / filename

    write_log(f"[INFO] Downloading {manifest['download_url']} to {dest}", log_file)
    log_and_print("Downloading update...", log_file, quiet)

    try:
        download(manifest["download_url"], dest)
    except Exception as e:
        write_log(f"[ERROR] Download failed: {e}", log_file)
        # Clean up partial file
        try:
            if dest.exists():
                dest.unlink()
        except Exception:
            pass
        if args.update_poc:
            poc_rc = run_poc_updates(args, log_file)
            return poc_rc if poc_rc != 0 else 4
        return 4

    # --- SHA256 verification ---
    expected_sha = manifest["sha256"].lower()
    actual_sha = sha256_file(dest)
    if expected_sha != actual_sha.lower():
        write_log(
            f"[ERROR] SHA256 MISMATCH — expected: {expected_sha}, got: {actual_sha}",
            log_file,
        )
        # Delete the unverified binary
        try:
            dest.unlink()
        except Exception:
            pass
        log_and_print("SHA256 verification failed. Update aborted.", log_file, quiet)
        if args.update_poc:
            poc_rc = run_poc_updates(args, log_file)
            return poc_rc if poc_rc != 0 else 5
        return 5

    write_log(f"[INFO] SHA256 verified: {actual_sha}", log_file)

    # --- Run installer (BLOCKING) ---
    log_and_print("Launching installer...", log_file, quiet)
    exit_code = run_installer(dest, quiet, log_file)

    if exit_code != 0:
        write_log(
            f"[ERROR] Installer exited with code {exit_code}. Services may be stopped.",
            log_file,
        )
        if args.update_poc:
            run_poc_updates(args, log_file)
        return 6

    write_log("[INFO] Installer completed successfully.", log_file)

    # --- PoC updates ---
    if args.update_poc:
        poc_rc = run_poc_updates(args, log_file)
        if poc_rc != 0:
            return poc_rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
