#!/usr/bin/env python3
"""
FryNetworks Miner Installer - Main Entry Point

This installer provides:
- Automatic miner type detection from keys
- Cross-platform service management
- Dependency installation and validation  
- Conflict detection and resolution
- FryNetworks corporate branding

Usage:
    python installer_main.py --gui                    # Launch GUI installer
    python installer_main.py install --key {key}     # CLI installation
    python installer_main.py validate --key {key}    # Key validation
    python installer_main.py service --action {action} # Service management
"""

import sys
import argparse
import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional

_logger = logging.getLogger(__name__)
_logger.addHandler(logging.NullHandler())

# Ensure bundled modules (core/, gui/) are importable both from source and PyInstaller onefile
_here = Path(__file__).parent
if getattr(sys, "frozen", False):
    _meipass = Path(getattr(sys, "_MEIPASS", _here))
    for extra in (_meipass, _meipass / "core", _meipass / "gui"):
        sys.path.insert(0, str(extra))
else:
    for extra in (_here, _here / "core", _here / "gui"):
        sys.path.insert(0, str(extra))

# Import all local modules at the top level so PyInstaller can detect them
try:
    from core.key_parser import MinerKeyParser
    from core.conflict_detector import ConflictDetector
    from core.service_manager import ServiceManager
    from core.config_manager import ConfigManager
    from core.binary_downloader import BinaryDownloader
    from gui.installer_window import FryNetworksInstallerWindow
    from tools.external_api import ExternalApiClient
    from tools.banner import TopBanner
    from tools.theme import Theme
except ImportError as e:
    print(f"Warning: Failed to import some modules: {e}")
    # Continue anyway - we'll try to import them again later

def main():
    """Main entry point for the installer."""
    # Load environment variables
    load_env()
    
    parser = argparse.ArgumentParser(
        description="Fry Networks Miner Installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --gui                           # Launch GUI installer
  %(prog)s install --key BM-ABC123...     # Install Bandwidth Miner
  %(prog)s validate --key ISM-DEF456...   # Validate satellite miner key
  %(prog)s service --action status        # Check service status
  %(prog)s uninstall --miner-code BM      # Uninstall specific miner
        """
    )
    
    # Global options
    parser.add_argument('--gui', action='store_true',
                       help='Launch graphical installer interface')
    parser.add_argument('--version', action='version', version='Fry Networks Installer 1.0.0')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose output')
    parser.add_argument('--quiet', action='store_true',
                       help='Suppress dialogs; used by updater for silent upgrade')
    
    # Subcommands
    subparsers = parser.add_subparsers(dest='command', help='Available commands')
    
    # Install command
    install_parser = subparsers.add_parser('install', help='Install a miner')
    install_parser.add_argument('--key', required=True,
                              help='Miner key (format: {CODE}-{32 chars})')
    install_parser.add_argument('--system-wide', action='store_true',
                              help='Install system-wide (requires admin/sudo)')
    install_parser.add_argument('--with-deps', action='store_true', default=True,
                              help='Install required dependencies')
    install_parser.add_argument('--with-optional', action='store_true',
                              help='Install optional tools')
    install_parser.add_argument('--auto-start', action='store_true', default=True,
                              help='Configure service to start automatically')
    install_parser.add_argument('--resolve-conflicts', choices=['replace', 'abort', 'force'],
                              default='abort', help='How to handle conflicts')
    class _DeprecatedTosAlias(argparse.Action):
        def __call__(self, parser, namespace, values, option_string=None):
            if option_string == "--accept-mysterium-tos":
                sys.stderr.write(
                    "DeprecationWarning: --accept-mysterium-tos is deprecated. "
                    "Use --accept-mystnodes-sdk-tos. Both still work this release.\n"
                )
            setattr(namespace, self.dest, True)

    install_parser.add_argument(
        '--accept-mysterium-tos', '--accept-mystnodes-sdk-tos',
        dest='accept_tos', action=_DeprecatedTosAlias, nargs=0,
        help='Accept MystNodes SDK terms of service (--accept-mysterium-tos: deprecated alias)',
    )
    install_parser.add_argument('--quiet', action='store_true',
                              help='Suppress dialogs; defer TOS to GUI catch-up')
    
    # Validate command  
    validate_parser = subparsers.add_parser('validate', help='Validate a miner key')
    validate_parser.add_argument('--key', required=True,
                               help='Miner key to validate')
    validate_parser.add_argument('--check-conflicts', action='store_true',
                               help='Check for installation conflicts')
    validate_parser.add_argument('--check-online', action='store_true',
                               help='Validate key with online services')
    
    # Service management command
    service_parser = subparsers.add_parser('service', help='Manage miner services')
    service_parser.add_argument('--action', required=True,
                              choices=['start', 'stop', 'restart', 'status', 'logs'],
                              help='Service action to perform')
    service_parser.add_argument('--miner-code',
                              help='Specific miner code (auto-detect if not provided)')
    service_parser.add_argument('--lines', type=int, default=50,
                              help='Number of log lines to show (for logs action)')
    
    # Uninstall command
    uninstall_parser = subparsers.add_parser('uninstall', help='Uninstall a miner')
    uninstall_parser.add_argument('--miner-code', required=True,
                                help='Miner code to uninstall')
    uninstall_parser.add_argument('--system-wide', action='store_true',
                                help='Uninstall from system-wide location')
    uninstall_parser.add_argument('--remove-data', action='store_true',
                                help='Remove all data and configuration')
    
    # List command
    list_parser = subparsers.add_parser('list', help='List installed miners')
    list_parser.add_argument('--format', choices=['table', 'json'], default='table',
                           help='Output format')
    
    # Parse arguments
    args = parser.parse_args()
    
    # Handle no command or GUI request
    if not args.command or args.gui:
        return launch_gui(args)
    
    # Handle CLI commands
    try:
        if args.command == 'install':
            return handle_install(args)
        elif args.command == 'validate':
            return handle_validate(args)
        elif args.command == 'service':
            return handle_service(args)
        elif args.command == 'uninstall':
            return handle_uninstall(args)
        elif args.command == 'list':
            return handle_list(args)
        else:
            parser.error(f"Unknown command: {args.command}")
    
    except KeyboardInterrupt:
        print("\\nInstaller interrupted by user")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 1


def _rotate_startup_log(log_path: Path, max_runs: int = 5) -> None:
    """Rotate startup log file, keeping the last *max_runs* runs."""
    if not log_path.exists():
        return
    try:
        base = str(log_path)
        oldest = Path(f"{base}.{max_runs}")
        if oldest.exists():
            oldest.unlink()
        for i in range(max_runs - 1, 0, -1):
            src = Path(f"{base}.{i}")
            dst = Path(f"{base}.{i + 1}")
            if src.exists():
                src.rename(dst)
        log_path.rename(Path(f"{base}.1"))
    except Exception:
        pass


def _setup_startup_logger() -> logging.Logger:
    """Set up a dedicated startup logger with run-based rotation (last 5 runs)."""
    import tempfile

    logger = logging.getLogger("startup")
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    local_app = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA')
    if local_app:
        log_dir = Path(local_app) / "FryNetworks"
    else:
        log_dir = Path(tempfile.gettempdir()) / "FryNetworks"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "startup_debug.log"
        _rotate_startup_log(log_path, max_runs=5)
        handler = logging.FileHandler(str(log_path), mode='a', encoding='utf-8')
        handler.setFormatter(logging.Formatter(
            '[%(asctime)s.%(msecs)03d] [STARTUP] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(handler)
    except Exception:
        pass

    return logger


def _read_installed_installer_version() -> Optional[str]:
    """Return DisplayVersion of installed FryNetworks Installer, or None.
    Scans HKLM/HKCU x 64-bit/32-bit registry views."""
    if not sys.platform.startswith("win"):
        return None
    try:
        import winreg
        path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
        roots = [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]
        views = [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]
        for root in roots:
            for view in views:
                try:
                    with winreg.OpenKey(root, path, 0, winreg.KEY_READ | view) as key:
                        i = 0
                        while True:
                            try:
                                subkey_name = winreg.EnumKey(key, i)
                                with winreg.OpenKey(key, subkey_name, 0,
                                                    winreg.KEY_READ | view) as sub:
                                    try:
                                        dn, _ = winreg.QueryValueEx(sub, "DisplayName")
                                        if ("frynetworks" in dn.lower()
                                                and "installer" in dn.lower()):
                                            dv, _ = winreg.QueryValueEx(sub, "DisplayVersion")
                                            return str(dv)
                                    except OSError:
                                        pass
                                i += 1
                            except OSError:
                                break
                except OSError:
                    continue
    except ImportError:
        pass
    return None


def _compare_versions(a: str, b: str) -> int:
    """Return -1 if a<b, 0 if equal, +1 if a>b. Compares numeric tuples.

    Inlined here (not imported from tools.updater) because the installer
    PyInstaller spec does not bundle tools/ as an importable package.
    """
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


def _self_downgrade_check():
    """L2: refuse to run if a newer FryNetworks Installer is already installed.

    Runs BEFORE QApplication construction so the GUI never flashes
    on-screen in the refusal case.
    """
    try:
        from version import WINDOWS_VERSION
    except Exception:
        return  # Can't check; don't block.
    installed = _read_installed_installer_version()
    if not installed:
        return  # No prior install.
    embedded = WINDOWS_VERSION
    # normalize both for comparison
    inst_v = installed if installed.startswith("v") else f"v{installed}"
    emb_v = embedded if embedded.startswith("v") else f"v{embedded}"
    if _compare_versions(inst_v, emb_v) <= 0:
        return  # Upgrade or same version — proceed.
    # Downgrade detected: show dialog and exit.
    from PySide6 import QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    msg = QtWidgets.QMessageBox()
    msg.setIcon(QtWidgets.QMessageBox.Icon.Critical)
    msg.setWindowTitle("Cannot Install Older Version")
    msg.setText(
        f"Cannot install Fry Networks Installer v{embedded}.\n\n"
        f"A newer version (v{installed}) is already installed.\n"
        "Please use your currently installed version or download\n"
        "the latest release from:\n"
        "https://github.com/Fry-Foundation/HardwareInstaller-Public/releases"
    )
    msg.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Ok)
    msg.exec()
    sys.exit(2)


def launch_gui(args):
    """Launch the graphical installer interface."""
    # Hide console window on Windows for GUI mode to prevent duplicate icons
    if sys.platform.startswith('win'):
        try:
            import ctypes
            # SW_HIDE = 0: Hide the console window
            ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
        except Exception:
            pass  # Non-critical if this fails

    _slog = _setup_startup_logger()
    _slog.info("=" * 80)
    _slog.info("NEW RUN — launch_gui() entered")
    _slog.info(f"sys.argv={sys.argv}, frozen={getattr(sys, 'frozen', False)}")
    try:
        import pyi_splash
        pyi_splash.update_text('Initializing...')
    except ImportError:
        pass

    try:
        # Check for GUI dependencies
        try:
            from PySide6 import QtWidgets, QtCore, QtGui, QtNetwork
            _slog.info("PySide6 imported successfully")
        except ImportError:
            print("Error: PySide6 not available for GUI mode")
            print("Please install PySide6 or use CLI mode:")
            print("  pip install PySide6")
            _slog.error("PySide6 import failed")
            return 1

        _self_downgrade_check()

        # Single-instance guard with communication to close old instances
        # This ensures that when a new version is launched (e.g., after update),
        # the old version exits gracefully
        server_name = "FryNetworksInstallerServer"
        shared_mem_key = "FryNetworksInstallerSharedMem"
        
        # Try to connect to an existing instance
        socket = QtNetwork.QLocalSocket()
        socket.connectToServer(server_name)
        
        if socket.waitForConnected(500):
            # Another instance is running - send quit command and wait for it to exit
            try:
                socket.write(b"QUIT")
                socket.flush()
                socket.waitForBytesWritten(1000)
                socket.disconnectFromServer()
                
                # Wait up to 3 seconds for the old instance to exit
                import time
                for _ in range(30):
                    time.sleep(0.1)
                    test_socket = QtNetwork.QLocalSocket()
                    test_socket.connectToServer(server_name)
                    if not test_socket.waitForConnected(100):
                        # Old instance has exited
                        break
                    test_socket.disconnectFromServer()
            except Exception:
                pass  # Best effort
        
        # Clean up any stale server socket
        QtNetwork.QLocalServer.removeServer(server_name)

        # Create our server to listen for new instances
        local_server = QtNetwork.QLocalServer()
        if not local_server.listen(server_name):
            # If we still can't create the server, try removing stale socket and retry
            QtNetwork.QLocalServer.removeServer(server_name)
            if not local_server.listen(server_name):
                print("Warning: Could not establish single-instance server")
                # Continue anyway - don't block the user

        _slog.info("Single-instance guard completed")

        # Import GUI components
        from gui.installer_window import FryNetworksInstallerWindow

        # Create and run GUI application
        app = QtWidgets.QApplication(sys.argv)
        _slog.info("QApplication created")
        app.setApplicationName("FryNetworks Installer")
        app.setApplicationVersion("1.0.0")
        
        # Set Windows AppUserModelID to prevent duplicate taskbar icons
        # This ensures all installer windows are grouped under a single icon
        if sys.platform.startswith('win'):
            try:
                import ctypes
                # Set the AppUserModelID to a unique identifier for this application
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('com.frynetworks.installer')
            except Exception:
                pass  # Non-critical if this fails
        
        # Enable high DPI scaling support
        app.setAttribute(QtCore.Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
        app.setAttribute(QtCore.Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
        
        # Set application icon - check if running from PyInstaller bundle
        if getattr(sys, 'frozen', False):
            base_path = Path(sys._MEIPASS)  # type: ignore
            icon_path = base_path / "resources" / "frynetworks_logo.ico"
        else:
            icon_path = Path(__file__).parent / "resources" / "frynetworks_logo.ico"
        
        if icon_path.exists():
            app.setWindowIcon(QtGui.QIcon(str(icon_path)))

        _slog.info("About to create FryNetworksInstallerWindow")
        window = FryNetworksInstallerWindow()
        window._quiet_mode = getattr(args, 'quiet', False)
        _slog.info("FryNetworksInstallerWindow created successfully (quiet=%s)", window._quiet_mode)
        
        # Connect the local server to handle quit requests from new instances
        def handle_new_connection():
            """Handle connection from a new instance trying to start."""
            client_socket = local_server.nextPendingConnection()
            if client_socket:
                def read_data():
                    data = client_socket.readAll()
                    if data and b"QUIT" in bytes(data):
                        # New instance is asking us to quit
                        print("New installer instance detected - exiting old instance...")
                        window._allow_close = True
                        tray = getattr(window, '_tray_icon', None)
                        if tray:
                            tray.hide()
                        window.close()
                        app.quit()
                    client_socket.disconnectFromServer()
                
                client_socket.readyRead.connect(read_data)
                # Also read immediately in case data already arrived
                if client_socket.bytesAvailable() > 0:
                    read_data()
        
        local_server.newConnection.connect(handle_new_connection)

        _slog.info("Calling window.show()")
        window.show()
        # Dismiss PyInstaller splash now that main window is visible
        try:
            import pyi_splash
            pyi_splash.update_text('Starting installer...')
            pyi_splash.close()
        except ImportError:
            pass  # not a frozen PyInstaller bundle (dev mode)
        _slog.info(f"window.show() returned — isVisible={window.isVisible()}, "
                    f"isMinimized={window.isMinimized()}, "
                    f"windowHandle={'exists' if window.windowHandle() else 'None'}")

        # Bring window to foreground (matches _restore_from_tray behaviour)
        window.raise_()
        _slog.info("window.raise_() called")
        window.activateWindow()
        _slog.info("window.activateWindow() called")

        # Win32 foreground activation for elevated (UAC) processes
        if sys.platform.startswith('win'):
            try:
                import ctypes
                hwnd = int(window.winId())
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                _slog.info(f"SetForegroundWindow(hwnd={hwnd}) called")
            except Exception as e:
                _slog.warning(f"SetForegroundWindow failed: {e}")

        _slog.info(f"Post-activation — isVisible={window.isVisible()}, "
                    f"isMinimized={window.isMinimized()}, "
                    f"pos=({window.x()},{window.y()}), "
                    f"size=({window.width()}x{window.height()})")
        _slog.info("Entering app.exec() event loop")

        return app.exec()
        
    except Exception as e:
        print(f"Failed to launch GUI: {e}")
        return 1


def handle_install(args):
    """Handle installation command."""
    return install_miner(args)
from core.key_parser import MinerKeyParser
from core.conflict_detector import ConflictDetector
from core.service_manager import ServiceManager
from core.config_manager import ConfigManager

# Import external API client from tools package
from tools.external_api import ExternalApiClient, _BUILD_CONFIG, get_external_api_client
from core.mystnodes_sdk_provisioning import provision_mystnodes_sdk_at_install, cleanup_mystnodes_sdk_on_failure
from core.upgrade_from_myst import upgrade_from_myst_at_install


def load_env():
    """Load environment variables from .env file next to the executable (runtime override)."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip().rstrip('/')
                    if key == 'EXTERNAL_API_BASE_URL' and value:
                        os.environ[key] = value
                        _logger.info(f"[load_env] Set EXTERNAL_API_BASE_URL to: {value}")
                    else:
                        os.environ.setdefault(key, value)


def get_api_base_url() -> str:
    """Get API base URL from environment variable."""
    api_url = os.getenv('EXTERNAL_API_BASE_URL', 'https://hardwareapi.frynetworks.com')
    return api_url


def _get_install_id() -> str:
    """Get or create install_id for this installation."""
    # REMOVED: Reading/writing plaintext install_id.txt
    # Only encrypted files are used for security
    import uuid
    return str(uuid.uuid4())


def acquire_miner_lease(api_client: ExternalApiClient, miner_key: str, install_id: str, miner_code: str = "") -> Dict[str, Any]:
    """
    Acquire lease for miner key using External API.

    Args:
        api_client: External API client instance
        miner_key: The full miner key
        install_id: Installation UUID
        miner_code: Miner type code (e.g., "BM", "HG", "MYST"). Detects external IP
                     and enforces IP limits based on version metadata.

    Returns:
        Dictionary with lease acquisition result and status
    """
    try:
        # Always detect external IP so the backend can track installations by IP for all miners
        from core.service_manager import get_external_ip
        external_ip = None
        ip_limit = None
        
        # Detect external IP for all miners (used for lease and device distribution tracking)
        try:
            external_ip = get_external_ip()
            print(f"🌐 Detected external IP: {external_ip}")
        except Exception as e:
            print(f"⚠ Could not detect external IP: {e}")

        # Check if this miner type has IP enforcement enabled
        if miner_code:
            try:
                # Query without platform filter to get root-level limit field
                version_data = api_client.get_required_version(miner_code, platform=None)
                ip_limit = version_data.get("limit")

                # If limit exists and is not "no", enforce IP checking
                if ip_limit is not None and ip_limit != "no":
                    # Special case: limit=0 means miner type is disabled
                    try:
                        limit_int = int(ip_limit) if isinstance(ip_limit, str) else ip_limit
                    except (ValueError, TypeError):
                        limit_int = None

                    if limit_int == 0:
                        miner_name = {
                            "BM": "Bandwidth Miner",
                            "MYST": "Mysterium"
                        }.get(miner_code, miner_code)

                        return {
                            "success": False,
                            "error": "miner_disabled",
                            "message": f"{miner_name} installations are currently disabled.",
                            "resolution": "This miner type has been temporarily disabled. Please contact support or check for updates.",
                            "limit": 0
                        }

                    if external_ip:
                        # Check current IP usage for this miner type
                        ip_status = api_client.check_ip_status(external_ip)
                        installations_by_type = ip_status.get("installations_by_type", {})
                        current_usage = installations_by_type.get(miner_code, {})

                        current_count = current_usage.get("count", 0)
                        limit_value = current_usage.get("limit", ip_limit)

                        # Check if limit is reached
                        if isinstance(limit_value, int) and current_count >= limit_value:
                            details = current_usage.get("details", [])
                            conflicting_keys = [d.get("miner_key", "Unknown") for d in details]

                            miner_name = {
                                "BM": "Bandwidth Miner",
                                "MYST": "Mysterium"
                            }.get(miner_code, miner_code)

                            return {
                                "success": False,
                                "error": "ip_limit_reached",
                                "message": f"IP limit reached: {current_count}/{limit_value} {miner_name} installation(s) already active on your network (IP: {external_ip}).",
                                "resolution": f"Only {limit_value} {miner_name} installation(s) allowed per IP address. "
                                             f"Conflicting installations: {', '.join(conflicting_keys[:3])}{'...' if len(conflicting_keys) > 3 else ''}",
                                "external_ip": external_ip,
                                "conflicting_keys": conflicting_keys,
                                "current_count": current_count,
                                "limit": limit_value
                            }
                    else:
                        # IP detection failed but limit is set — block to be safe
                        return {
                            "success": False,
                            "error": "ip_check_failed",
                            "message": "Cannot detect external IP address for IP limit enforcement.",
                            "resolution": "Please check your internet connection and try again."
                        }

            except Exception as e:
                # If IP limit checking is configured but fails, block installation
                if ip_limit is not None and ip_limit != "no":
                    return {
                        "success": False,
                        "error": "ip_check_failed",
                        "message": f"Cannot validate IP availability: {e}",
                        "resolution": "Please check your internet connection and try again."
                    }

        # First check current lease status
        lease_status = api_client.lease_status(miner_key)

        active = lease_status.get("active", False)
        holder_install_id = lease_status.get("holder_install_id")

        print(f"🔍 Checking lease status for {miner_key}...")

        if active and holder_install_id and holder_install_id != install_id:
            # Another device holds an active lease
            return {
                "success": False,
                "error": "active_lease_held",
                "message": f"Miner key is currently active on another device (Install ID: {holder_install_id})",
                "resolution": "Stop the miner on the other device before installing here",
                "holder_install_id": holder_install_id
            }

        elif not active and holder_install_id and holder_install_id != install_id:
            # Another device holds lease but it's inactive - can be taken over
            print(f"⚠ Found inactive lease held by {holder_install_id}")
            print("🔄 Lease is available for takeover (device migration)")

        # Try to acquire the lease
        print(f"🔐 Acquiring lease for {miner_key}...")
        lease_result = api_client.acquire_installation_lease(miner_key, install_id, lease_seconds=3600, external_ip=external_ip)
        lease_granted = lease_result.get("granted", False) if isinstance(lease_result, dict) else bool(lease_result)
        error_code = lease_result.get("error_code") if isinstance(lease_result, dict) else None

        if lease_granted:
            print(f"✅ Lease acquired successfully")
            return {
                "success": True,
                "message": "Lease acquired - installation can proceed",
                "install_id": install_id,
                "external_ip": external_ip,
                "takeover": bool(holder_install_id and holder_install_id != install_id)
            }
        else:
            if error_code == "IP_LIMIT_REACHED":
                miner_name = {
                    "BM": "Bandwidth Miner",
                    "MYST": "Mysterium"
                }.get(miner_code, miner_code)
                return {
                    "success": False,
                    "error": "ip_limit_reached",
                    "message": f"Installation blocked: IP limit reached for {miner_name} on your network.",
                    "resolution": f"Check the allowed limit for {miner_code} installations per IP address."
                }
            return {
                "success": False,
                "error": "lease_denied",
                "message": "Lease acquisition was denied by the server",
                "resolution": "Another installation may have acquired the lease first"
            }

    except Exception as e:
        return {
            "success": False,
            "error": "api_error",
            "message": f"Lease acquisition failed: {e}",
            "resolution": "Check network connection and API availability"
        }


def install_miner(args):
    """Install a miner with the given arguments."""
    # Validate key and detect miner type
    parser = MinerKeyParser()
    key_info = parser.parse_miner_key(args.key)
    
    if not key_info["valid"]:
        print(f"Error: {key_info['error']}")
        return 1
    
    print(f"Detected miner: {key_info['name']} ({key_info['code']})")
    is_bandwidth_miner = key_info["code"] == "BM"
    # Public build: Mysterium is the sole partner integration (mandatory for BM)

    # Check for conflicts with External API
    api_client = get_external_api_client()
    print(f"✓ External API connected: {api_client.base_url}")
    
    detector = ConflictDetector(api_client=api_client)
    conflicts = detector.check_device_conflicts(args.key)
    
    if conflicts.get("error"):
        print(f"\\nValidation error: {conflicts['error']}")
        return 1
    
    if conflicts.get("has_conflicts"):
        print("\\nConflicts detected:")
        for detail in conflicts["details"]:
            print(f"  • {detail['message']}")
        
        if args.resolve_conflicts == "abort":
            print("\\nInstallation aborted due to conflicts")
            print("Use --resolve-conflicts to override")
            return 1
        elif args.resolve_conflicts == "replace":
            print("\\nResolving conflicts...")
            resolution = detector.resolve_conflicts(conflicts, "replace")
            if not resolution["success"]:
                print("Failed to resolve conflicts")
                return 1
    
    # Acquire lease for the miner key
    install_id = _get_install_id()
    print(f"\\n🔐 Lease Acquisition Phase")
    print(f"Install ID: {install_id}")
    
    lease_result = acquire_miner_lease(api_client, args.key, install_id, miner_code=key_info["code"])
    
    if not lease_result["success"]:
        print(f"\\n❌ Lease acquisition failed:")
        print(f"  Error: {lease_result['message']}")
        
        if lease_result.get("resolution"):
            print(f"  Solution: {lease_result['resolution']}")
        
        if lease_result.get("error") == "active_lease_held":
            print(f"\\n📱 Another device is actively using this miner key.")
            print(f"   Holder: {lease_result.get('holder_install_id', 'Unknown')}")
            print(f"   Action: Stop the miner on the other device first.")
        
        return 1
    
    print(f"\\n✅ {lease_result['message']}")
    if lease_result.get("takeover"):
        print("🔄 This installation will take over from an inactive device")
    
    # Setup configuration
    config_manager = ConfigManager(key_info["code"])
    
    print("\\nSetting up installation directories...")
    setup_result = config_manager.setup_directories(args.system_wide)
    if not setup_result["success"]:
        print("Failed to setup directories:")
        for error in setup_result["errors"]:
            print(f"  • {error}")
        return 1
    
    # Write configuration
    print("Writing configuration...")
    write_result = config_manager.write_miner_key(args.key, args.system_wide)
    if not write_result["success"]:
        print("Failed to write configuration:")
        for error in write_result["errors"]:
            print(f"  • {error}")
        return 1
    
    # Install dependencies if requested
    if args.with_deps:
        print("\\nInstalling dependencies...")
        # TODO: Implement dependency installation
        print("Dependency installation not yet implemented")
    
    # MystNodes SDK TOS handling (Track 4 — was Mysterium TOS in Track 3)
    accept_tos = getattr(args, 'accept_tos', False)
    quiet = getattr(args, 'quiet', False)
    if is_bandwidth_miner and quiet and not accept_tos:
        sdk_opt_in = False  # defer to GUI catch-up
    elif is_bandwidth_miner and accept_tos:
        sdk_opt_in = True
    else:
        sdk_opt_in = False  # non-BM always False

    # Install service
    print("\\nInstalling service...")
    service_manager = ServiceManager(key_info["code"])
    install_result = service_manager.install_service(
        args.key,
        auto_start=args.auto_start,
        system_wide=args.system_wide,
        sdk_opt_in=sdk_opt_in,
        _stage_partner_sdks={"mystnodes_sdk": True} if sdk_opt_in else {},
    )

    if install_result["success"]:
        print(f"✓ {install_result['message']}")
        for action in install_result.get("actions", []):
            print(f"  • {action}")

        # Write tos_state.json (Track 3) — read-before-write to preserve existing acceptance
        if is_bandwidth_miner:
            from core.tos_state import write_tos_state, read_tos_state, is_resolved_accept
            config_dir = config_manager.get_installation_directory(
                getattr(args, 'system_wide', False)
            ) / "config"
            if quiet and not accept_tos:
                existing = read_tos_state(config_dir)
                if not is_resolved_accept(existing):
                    write_tos_state(config_dir, accepted_via="installer-quiet-deferred",
                                    tos_pending_catchup=True)
            elif accept_tos:
                write_tos_state(config_dir, accepted_via="installer-interactive")

            if sdk_opt_in and is_bandwidth_miner:
                base_dir = config_dir.parent
                nssm_path = base_dir / "nssm.exe"

                if not nssm_path.exists():
                    print("✗ MystNodes SDK provisioning skipped — nssm.exe missing")
                    return 1

                # Legacy Mysterium teardown (Fix #2b) — must run before SDK provisioning
                print("→ Checking for legacy Mysterium installation...")
                upgrade_result = upgrade_from_myst_at_install(
                    install_root=base_dir,
                    nssm_path=nssm_path,
                    progress_callback=lambda msg: print(f"  {msg}"),
                )
                if upgrade_result.failed:
                    print(f"✗ Legacy Mysterium upgrade failed: {upgrade_result.error}")
                    return 1
                if upgrade_result.upgrade_performed:
                    print("✓ Legacy Mysterium teardown complete")

                print("→ Provisioning MystNodes SDK Client...")
                result = provision_mystnodes_sdk_at_install(
                    install_root=base_dir,
                    nssm_path=nssm_path,
                    progress_callback=lambda label, status: print(f"  [{label}] {status}"),
                )
                if not result.success:
                    print(f"✗ MystNodes SDK provisioning failed at step '{result.step}': {result.error}")
                    cleanup_mystnodes_sdk_on_failure(base_dir, nssm_path)
                    return 1
                print("✓ MystNodes SDK provisioning complete")

        return 0
    else:
        print(f"✗ {install_result['message']}")
        return 1


def handle_validate(args):
    """Handle validation command."""
    from core.key_parser import MinerKeyParser
    from core.conflict_detector import ConflictDetector
    
    # Parse key format first
    parser = MinerKeyParser()
    result = parser.parse_miner_key(args.key)
    
    if not result["valid"]:
        print(f"✗ Invalid key format: {result['error']}")
        return 1
    
    print(f"✓ Valid {result['name']} key format")
    print(f"  Code: {result['code']}")
    print(f"  Group: {result['group']}")
    if result["exclusive"]:
        print(f"  Exclusive with: {result['exclusive']}")
    
    # Validate with External API
    try:
        api_client = get_external_api_client()
        
        print(f"\\n🔍 Validating with External API...")
        miner_profile = api_client.get_miner_profile(args.key)
        
        if miner_profile.get("exists", False):
            print(f"✓ Miner key exists in system")
            
            # Show additional profile info if available
            if miner_profile.get("registered_mac"):
                print(f"  Registered MAC: {miner_profile['registered_mac']}")
            if miner_profile.get("hex_id"):
                print(f"  Hex ID: {miner_profile['hex_id']}")
                
        else:
            print(f"✗ Miner key does not exist in system")
            print("  Contact support or verify the key is correct")
            return 1
            
    except Exception as e:
        print(f"✗ External API validation failed: {e}")
        print("  Check network connection and API availability")
        return 1
    
    # Check conflicts if requested
    if args.check_conflicts:
        print("\\n🔍 Checking for conflicts...")
        try:
            detector = ConflictDetector(api_client)
            conflicts = detector.check_device_conflicts(args.key)
            
            if conflicts.get("error"):
                print(f"✗ Validation error: {conflicts['error']}")
                return 1
            elif conflicts.get("has_conflicts"):
                print("⚠ Conflicts detected:")
                for detail in conflicts["details"]:
                    severity_icon = "🔥" if detail["severity"] == "error" else "⚠"
                    print(f"  {severity_icon} {detail['message']}")
            else:
                print("✓ No conflicts detected - ready for installation")
        except Exception as e:
            print(f"✗ Conflict check failed: {e}")
            return 1
    
    return 0


def handle_service(args):
    """Handle service management command."""
    from core.config_manager import ConfigManager
    from core.service_manager import ServiceManager
    
    # Auto-detect miner code if not provided
    miner_code = args.miner_code
    if not miner_code:
        config_manager = ConfigManager()
        installations = config_manager.detect_existing_installations()
        
        if not installations:
            print("No miner installations found")
            return 1
        elif len(installations) == 1:
            miner_code = installations[0]["miner_code"]
            print(f"Auto-detected miner: {installations[0]['miner_name']}")
        else:
            print("Multiple miners found, please specify --miner-code:")
            for install in installations:
                print(f"  • {install['miner_code']}: {install['miner_name']}")
            return 1
    
    service_manager = ServiceManager(miner_code)
    
    if args.action == "status":
        status = service_manager.get_service_status()
        print(f"Service status: {status}")
        
    elif args.action == "start":
        result = service_manager.start_service()
        print(f"{'✓' if result['success'] else '✗'} {result['message']}")
        
    elif args.action == "stop":
        result = service_manager.stop_service()
        print(f"{'✓' if result['success'] else '✗'} {result['message']}")
        
    elif args.action == "restart":
        stop_result = service_manager.stop_service()
        if stop_result["success"]:
            start_result = service_manager.start_service()
            print(f"{'✓' if start_result['success'] else '✗'} Service restarted")
        else:
            print(f"✗ Failed to stop service: {stop_result['message']}")
            
    elif args.action == "logs":
        logs = service_manager.get_service_logs(args.lines)
        if logs["stdout"]:
            print("=== Service Logs ===")
            print(logs["stdout"])
        if logs["stderr"]:
            print("=== Error Logs ===")
            print(logs["stderr"])
    
    return 0


def handle_uninstall(args):
    """Handle uninstall command."""
    from core.service_manager import ServiceManager
    from core.config_manager import ConfigManager
    
    print(f"Uninstalling {args.miner_code} miner...")
    
    # Stop and remove service
    service_manager = ServiceManager(args.miner_code)
    result = service_manager.uninstall_service()
    
    if result["success"]:
        print(f"✓ {result['message']}")
        for action in result.get("actions", []):
            print(f"  • {action}")
    else:
        print(f"⚠ Service removal: {result['message']}")
    
    # Remove configuration if requested
    if args.remove_data:
        config_manager = ConfigManager(args.miner_code)
        config_result = config_manager.remove_configuration(args.system_wide)
        
        if config_result["success"]:
            print("✓ Configuration and data removed")
        else:
            print(f"⚠ Configuration removal failed: {config_result['errors']}")
    
    return 0


def handle_list(args):
    """Handle list command."""
    from core.config_manager import ConfigManager
    
    config_manager = ConfigManager()
    installations = config_manager.detect_existing_installations()
    
    if not installations:
        print("No miner installations found")
        return 0
    
    if args.format == "json":
        import json
        print(json.dumps(installations, indent=2))
    else:
        print("Installed Miners:")
        print("-" * 60)
        for install in installations:
            scope = "System" if install["system_wide"] else "User"
            print(f"{install['miner_code']:4} | {install['miner_name']:25} | {scope}")
        print("-" * 60)
        print(f"Total: {len(installations)} installation(s)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
