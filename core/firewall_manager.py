"""Windows Firewall rule management for Fry Networks installer.

Uses PowerShell New-NetFirewallRule / Remove-NetFirewallRule via subprocess.
Idempotent: every add_rule() removes the existing rule with the same
DisplayName first, preventing duplicate accumulation.

Rule DisplayNames follow the existing "FryNetworks {service} {description}"
convention (no space in FryNetworks) for consistency with Mysterium-authored
rules in the firewall admin UI.
"""

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional


_PS_TIMEOUT_SECONDS = 30

_PS_LAUNCH = [
    'powershell.exe',
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-Command',
]


class FirewallManager:
    """Idempotent Windows Firewall rule management."""

    def __init__(self, debug_log: Optional[Callable[[str], None]] = None):
        self._debug_log = debug_log or (lambda _msg: None)

    def add_rule(self, display_name: str, program_path) -> bool:
        """Ensure Allow rules (inbound + outbound) exist for program_path.

        Returns True on success, False on any PowerShell failure.
        Idempotent: removes any existing rule with the same DisplayName
        first, then adds fresh rules.
        """
        program_str = str(program_path)
        if not Path(program_str).exists():
            self._debug_log(
                f"[firewall] skip {display_name!r}: binary missing at {program_str}"
            )
            return False

        self._debug_log(
            f"[firewall] ensuring rule {display_name!r} for {program_str}"
        )

        # Remove existing rule(s) with this DisplayName to prevent duplicates
        self.remove_rule(display_name, quiet=True)

        dn_escaped = display_name.replace("'", "''")
        prog_escaped = program_str.replace("'", "''")

        ok_in = self._run_ps(
            f"New-NetFirewallRule "
            f"-DisplayName '{dn_escaped}' "
            f"-Direction Inbound "
            f"-Action Allow "
            f"-Program '{prog_escaped}' "
            f"-Profile Any "
            f"-ErrorAction Stop | Out-Null"
        )

        ok_out = self._run_ps(
            f"New-NetFirewallRule "
            f"-DisplayName '{dn_escaped}' "
            f"-Direction Outbound "
            f"-Action Allow "
            f"-Program '{prog_escaped}' "
            f"-Profile Any "
            f"-ErrorAction Stop | Out-Null"
        )

        if ok_in and ok_out:
            self._debug_log(f"[firewall] added rule {display_name!r} (in+out)")
            return True
        self._debug_log(
            f"[firewall] partial failure adding {display_name!r}: "
            f"in={ok_in} out={ok_out}"
        )
        return False

    def remove_rule(self, display_name: str, quiet: bool = False) -> bool:
        """Remove ALL firewall rules matching DisplayName.

        Returns True if the operation succeeded or no rules existed.
        Returns False only on PowerShell execution failure.
        """
        dn_escaped = display_name.replace("'", "''")
        ok = self._run_ps(
            f"$r = Get-NetFirewallRule -DisplayName '{dn_escaped}' "
            f"-ErrorAction SilentlyContinue; "
            f"if ($r) {{ "
            f"  Remove-NetFirewallRule -DisplayName '{dn_escaped}' "
            f"-ErrorAction SilentlyContinue; "
            f"  Write-Output 'REMOVED' "
            f"}} else {{ Write-Output 'NONE' }}"
        )
        if not quiet:
            self._debug_log(f"[firewall] remove_rule {display_name!r}: ok={ok}")
        return ok

    def _run_ps(self, script: str) -> bool:
        """Run a single PowerShell one-liner. Return True if rc==0."""
        try:
            r = subprocess.run(
                _PS_LAUNCH + [script],
                capture_output=True,
                timeout=_PS_TIMEOUT_SECONDS,
                text=True,
            )
            if r.returncode != 0:
                stderr = (r.stderr or '').strip()[:500]
                self._debug_log(
                    f"[firewall] PS rc={r.returncode} stderr={stderr!r}"
                )
                return False
            return True
        except subprocess.TimeoutExpired:
            self._debug_log(f"[firewall] PS timeout after {_PS_TIMEOUT_SECONDS}s")
            return False
        except Exception as e:
            self._debug_log(f"[firewall] PS exception: {e!r}")
            return False

    # ------------- convenience helpers for installer call sites

    def add_miner_rules(self, miner_code: str, miner_dir) -> None:
        """Scan a miner directory for FRY_*.exe and add rules."""
        miner_dir = Path(miner_dir)
        if not miner_dir.exists():
            self._debug_log(
                f"[firewall] miner dir missing: {miner_dir} (code={miner_code})"
            )
            return
        code_lower = miner_code.lower()
        for exe in miner_dir.glob('FRY_*.exe'):
            name_lower = exe.name.lower()
            if name_lower.startswith(f'fry_poc_{code_lower}_'):
                label = 'PoC'
            elif name_lower.startswith(f'fry_{code_lower}_'):
                label = 'GUI'
            else:
                continue
            self.add_rule(f"FryNetworks {miner_code} {label}", exe)

    def remove_miner_rules(self, miner_code: str) -> None:
        """Remove both GUI and PoC firewall rules for a miner code."""
        for label in ('GUI', 'PoC'):
            self.remove_rule(f"FryNetworks {miner_code} {label}", quiet=True)

    def ensure_olostep_rule(self) -> None:
        """Add rule for Olostep Browser if installed."""
        olostep = Path(os.environ.get('LOCALAPPDATA', '')) / 'Olostep-Browser' / 'OlostepBrowser.exe'
        if olostep.exists():
            self.add_rule("FryNetworks Olostep Browser", olostep)

    def ensure_updater_rule(self) -> None:
        """Add rule for the updater if deployed."""
        updater = Path(r'C:\ProgramData\FryNetworks\updater\frynetworks_updater.exe')
        if updater.exists():
            self.add_rule("FryNetworks Updater", updater)
