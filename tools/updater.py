"""
FryNetworks Installer Updater

Checks the latest GitHub release for a newer MSI, downloads it, verifies (optional)
SHA256, and invokes msiexec to install. Designed to be run silently from a scheduled
task (per-user, non-elevated).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, cast


DEFAULT_REPO = "Fry-Foundation/HardwareInstaller-Public"
DEFAULT_TASK_NAME = "FryNetworksUpdater"
DEFAULT_EMBEDDED_TOKEN = os.getenv("EMBEDDED_GITHUB_TOKEN", "")
DEFAULT_POC_REPO = "Fry-Foundation/HardwarePoC_releases"


def log_path(custom: Optional[Path] = None) -> Path:
    if custom:
        return custom
    base = Path(os.getenv("LOCALAPPDATA", tempfile.gettempdir()))
    return base / "FryNetworks" / "Updater" / "updater.log"


def write_log(msg: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text((dest.read_text() if dest.exists() else "") + msg + "\n", encoding="utf-8")


def normalize_version(ver: str) -> str:
    ver = ver.strip()
    return ver if ver.startswith("v") else f"v{ver}"


def read_version_from_installer(dir_path: Path) -> Optional[str]:
    """
    Look for frynetworks_installer_v*.exe next to the updater and parse the version.
    """
    pattern = "frynetworks_installer_v*.exe"
    for candidate in dir_path.glob(pattern):
        name = candidate.name
        # expect frynetworks_installer_vX.Y.Z.exe
        if "_v" in name:
            part = name.split("_v", 1)[-1].rsplit(".", 1)[0]
            return normalize_version(part)
    return None


def fetch_json(url: str, token: Optional[str] = None) -> dict:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path, token: Optional[str] = None) -> None:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/octet-stream")
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1024 * 1024)
            if not chunk:
                break
            f.write(chunk)


def find_asset(release: dict, suffix: str) -> Optional[dict]:
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if name.lower().endswith(suffix.lower()):
            return asset
    return None


def find_installer_asset(release: dict) -> Optional[dict]:
    """Find the installer asset, preferring MSI over EXE, filtered to frynetworks_installer_* prefix."""
    candidates = []
    for asset in release.get("assets", []):
        name = asset.get("name", "").lower()
        if name.startswith("frynetworks_installer_"):
            candidates.append(asset)
    if not candidates:
        return None
    for c in candidates:
        if c.get("name", "").lower().endswith(".msi"):
            return c
    return candidates[0]


def discover_installer_version(cli_version: Optional[str], log_file: Path) -> Optional[str]:
    """
    Discover the installed version via a cascade:
      1. CLI --current-version argument
      2. ARP registry DisplayVersion for FryNetworks Installer
      3. Filename pattern (frynetworks_installer_v*.exe next to updater)
      4. Hard-fail (return None)
    """
    if cli_version:
        ver = normalize_version(cli_version)
        write_log(f"Version source: CLI argument -> {ver}", log_file)
        return ver

    if sys.platform.startswith("win"):
        try:
            import winreg
            uninstall_key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
            for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for view_flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    try:
                        with winreg.OpenKey(root, uninstall_key, 0,
                                            winreg.KEY_READ | view_flag) as key:
                            i = 0
                            while True:
                                try:
                                    subkey_name = winreg.EnumKey(key, i)
                                    with winreg.OpenKey(key, subkey_name, 0,
                                                        winreg.KEY_READ | view_flag) as subkey:
                                        try:
                                            display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                                            if "frynetworks" in display_name.lower() and "installer" in display_name.lower():
                                                display_ver, _ = winreg.QueryValueEx(subkey, "DisplayVersion")
                                                ver = normalize_version(str(display_ver))
                                                write_log(f"Version source: ARP registry -> {ver}", log_file)
                                                return ver
                                        except OSError:
                                            pass
                                    i += 1
                                except OSError:
                                    break
                    except OSError:
                        pass
        except ImportError:
            pass

    ver = read_version_from_installer(Path(__file__).resolve().parent)
    if ver:
        write_log(f"Version source: filename pattern -> {ver}", log_file)
        return ver

    write_log("CANNOT_DETERMINE_INSTALLER_VERSION: all discovery methods exhausted.", log_file)
    return None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run_installer(installer_path: Path, quiet: bool, log_file: Path) -> None:
    args = [str(installer_path)]
    if quiet:
        args.append("--quiet")
    write_log(f"Running: {' '.join(args)}", log_file)
    subprocess.Popen(args)


def find_poc_asset(release: dict, miner_code: str) -> Optional[dict]:
    """Find a PoC service asset for *miner_code* in *release*.

    Looks for ``FRY_PoC_{miner_code}_v*.exe`` among the release assets and
    extracts the version from the filename.  Returns a dict with keys
    ``name``, ``url``, ``version``, and ``sha_asset`` (the matching
    ``.sha256`` sidecar asset dict, or *None*), or *None* if no match.
    """
    prefix = f"FRY_PoC_{miner_code}_v".lower()
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if not name.lower().startswith(prefix) or not name.lower().endswith(".exe"):
            continue
        # Extract version: FRY_PoC_BM_v1.6.5.exe → 1.6.5
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
    """Scan *config_dir* for installed PoC miners.

    Looks for ``miner-*/config/installer_config.json`` under *config_dir* and
    returns a list of dicts with ``miner_code``, ``poc_version``,
    ``install_root``, ``nssm_path``, and ``config_path``.
    """
    installs = []
    for cfg in sorted(config_dir.glob("miner-*/config/installer_config.json")):
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
    """Stop → backup → swap → re-register → start a PoC service.

    Raises on failure so the caller can surface partial-update state.
    """
    miner_code = info["miner_code"]
    install_root = info["install_root"]
    nssm = str(info["nssm_path"])
    old_service = f"FRY_PoC_{miner_code}_v{info['poc_version']}"
    new_service = f"FRY_PoC_{miner_code}_v{new_version}"
    new_exe_path = install_root / f"FRY_PoC_{miner_code}_v{new_version}.exe"

    # 1. Stop
    subprocess.run(
        [nssm, "stop", old_service],
        check=True, capture_output=True, timeout=30,
    )
    write_log(f"STOPPED: {old_service}", log_file)

    # 2. Remove
    subprocess.run(
        [nssm, "remove", old_service, "confirm"],
        check=True, capture_output=True, timeout=15,
    )
    write_log(f"REMOVED: {old_service}", log_file)

    # 3. Backup old exe(s)
    ts = int(time.time())
    for old_exe in install_root.glob(f"FRY_PoC_{miner_code}_v*.exe"):
        bak = old_exe.parent / f"{old_exe.name}.bak.{ts}"
        shutil.copy2(str(old_exe), str(bak))
        write_log(f"BACKUP: {bak}", log_file)

    # 4. Install new exe
    shutil.copy2(str(new_exe_dest), str(new_exe_path))
    write_log(f"INSTALLED: {new_exe_path}", log_file)

    # 5. Register — convention-derived params (matches service_manager.py)
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
    write_log(f"REGISTERED: {new_service}", log_file)

    # 6. Start
    subprocess.run(
        [nssm, "start", new_service],
        check=True, capture_output=True, timeout=30,
    )
    write_log(f"STARTED: {new_service}", log_file)

    # 7. Update installer_config.json
    cfg_path = info["config_path"]
    try:
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_data["poc_version"] = new_version
        cfg_path.write_text(json.dumps(cfg_data, indent=2) + "\n", encoding="utf-8")
        write_log(f"CONFIG UPDATED: {cfg_path}", log_file)
    except Exception as exc:
        write_log(f"WARNING: config update failed ({cfg_path}): {exc}", log_file)


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
        write_log("No PoC installations found.", log_file)
        return 0

    for inst in installs:
        miner_code = inst["miner_code"]
        try:
            release = fetch_json(
                f"https://api.github.com/repos/{args.poc_repo}/releases/latest",
                token,
            )
        except Exception as exc:
            write_log(f"PoC release check failed for {miner_code}: {exc}", log_file)
            continue

        asset = find_poc_asset(release, miner_code)
        if not asset:
            write_log(f"No PoC asset for {miner_code} in latest release.", log_file)
            continue

        installed_ver = normalize_version(inst["poc_version"])
        remote_ver = normalize_version(asset["version"])
        if compare_versions(remote_ver, installed_ver) <= 0:
            write_log(
                f"PoC up to date for {miner_code}: {installed_ver}",
                log_file,
            )
            continue

        write_log(
            f"PoC update available for {miner_code}: {installed_ver} -> {remote_ver}",
            log_file,
        )

        if args.dry_run:
            write_log(f"[dry-run] Would update PoC for {miner_code}", log_file)
            continue

        # Download
        asset_url = asset["url"]
        if not asset_url:
            write_log(f"PoC asset for {miner_code} missing download URL.", log_file)
            continue
        dest = Path(tempfile.gettempdir()) / asset["name"]
        write_log(f"Downloading {asset_url} to {dest}", log_file)
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
                        f"PoC checksum mismatch for {miner_code}: "
                        f"expected {expected}, got {actual}",
                        log_file,
                    )
                    continue
                write_log(f"PoC checksum verified for {miner_code}.", log_file)

        # Apply update
        try:
            update_poc_service(inst, dest, asset["version"], log_file)
        except Exception as exc:
            write_log(
                f"ERROR: PoC update FAILED for {miner_code} "
                f"(service may be in partial state): {exc}",
                log_file,
            )
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update FryNetworks Installer from latest GitHub release.")
    p.add_argument("--repo", default=DEFAULT_REPO, help="GitHub repo owner/name (default: %(default)s)")
    p.add_argument("--current-version", help="Current version (e.g., v3.6.0). If omitted, infer from installer exe name in the updater directory.")
    p.add_argument("--token", help="GitHub token for higher rate limits/private repos (optional).")
    p.add_argument("--quiet", action="store_true", help="Install MSI silently (/qn).")
    p.add_argument("--log", type=Path, help="Log file path (default: %%LOCALAPPDATA%%/FryNetworks/Updater/updater.log).")
    p.add_argument("--dry-run", action="store_true", help="Do not download/install, just report actions.")
    p.add_argument("--update-poc", action="store_true",
                   help="Also check/update PoC service binaries.")
    p.add_argument("--poc-repo", default=DEFAULT_POC_REPO,
                   help="GitHub repo for PoC releases (default: %(default)s)")
    p.add_argument("--poc-token",
                   help="GitHub token for PoC repo (falls back to --token / GITHUB_TOKEN).")
    p.add_argument("--poc-config-dir", default=r"C:\ProgramData\FryNetworks",
                   help="Parent dir containing miner-* subdirs (default: %(default)s)")
    return p.parse_args()


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
    if ta < tb: return -1
    if ta > tb: return 1
    return 0


def main() -> int:
    args = parse_args()
    log_file = log_path(args.log)

    try:
        current_version = discover_installer_version(args.current_version, log_file)
        if not current_version:
            print("CANNOT_DETERMINE_INSTALLER_VERSION")
            if args.update_poc:
                write_log("Installer version unknown; proceeding with PoC update only.", log_file)
                return run_poc_updates(args, log_file)
            return 2
        write_log(f"Current version: {current_version}", log_file)
        print(f"Checking for updates... (current: {current_version})")

        token = args.token or os.environ.get("GITHUB_TOKEN") or DEFAULT_EMBEDDED_TOKEN or None

        release = fetch_json(f"https://api.github.com/repos/{args.repo}/releases/latest", token)
        remote_ver = normalize_version(release.get("tag_name", ""))
        write_log(f"Latest release: {remote_ver}", log_file)

        if not remote_ver:
            write_log(
                "Could not parse remote version. Refusing to update.",
                log_file,
            )
            print("Could not determine remote version.")
            if args.update_poc:
                return run_poc_updates(args, log_file)
            return 1

        cmp = compare_versions(remote_ver, current_version)
        if cmp <= 0:
            write_log(
                f"Remote v{remote_ver} is not newer than current v{current_version}. "
                "No update needed (downgrade refused).",
                log_file,
            )
            print("No update needed.")
            if args.update_poc:
                return run_poc_updates(args, log_file)
            return 0

        installer_asset = find_installer_asset(release)
        if not installer_asset:
            write_log("No installer asset found in latest release.", log_file)
            if args.update_poc:
                return run_poc_updates(args, log_file)
            return 1

        msi_url = installer_asset.get("browser_download_url")
        if not msi_url:
            write_log("Installer asset missing download URL.", log_file)
            if args.update_poc:
                return run_poc_updates(args, log_file)
            return 1
        msi_name = installer_asset.get("name", "update.exe")
        dest = Path(tempfile.gettempdir()) / msi_name

        if args.dry_run:
            write_log(f"[dry-run] Would download {msi_url} to {dest}", log_file)
            return 0

        write_log(f"Downloading {msi_url} to {dest}", log_file)
        print("Downloading update...")
        download(cast(str, msi_url), dest, token)

        sha_asset_name = (installer_asset.get("name", "") + ".sha256").lower()
        sha_asset = next(
            (a for a in release.get("assets", [])
             if a.get("name", "").lower() == sha_asset_name),
            None,
        )
        if sha_asset:
            sha_url = sha_asset.get("browser_download_url")
            if not sha_url:
                write_log("Checksum asset missing download URL; skipping checksum verification.", log_file)
            else:
                sha_dest = dest.with_suffix(dest.suffix + ".sha256")
                write_log(f"Downloading checksum {sha_url}", log_file)
                download(cast(str, sha_url), sha_dest, token)
                expected = sha_dest.read_text().split()[0].strip()
                actual = sha256_file(dest)
                if expected.lower() != actual.lower():
                    write_log(f"Checksum mismatch: expected {expected}, got {actual}", log_file)
                    if args.update_poc:
                        return run_poc_updates(args, log_file)
                    return 1
            write_log("Checksum verified.", log_file)

        print("Launching installer...")
        run_installer(dest, args.quiet, log_file)
        write_log("Update triggered (msiexec launched).", log_file)

        if args.update_poc:
            poc_result = run_poc_updates(args, log_file)
            if poc_result != 0:
                return poc_result

        return 0
    except urllib.error.HTTPError as e:
        write_log(f"HTTP error: {e}", log_file)
        return 1
    except Exception as e:  # noqa: BLE001
        write_log(f"Update failed: {e}", log_file)
        return 1


if __name__ == "__main__":
    sys.exit(main())
