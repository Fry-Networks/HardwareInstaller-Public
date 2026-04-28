"""Read/write helpers for tos_state.json — Mysterium TOS consent marker.

tos_state.json is a plaintext JSON file stored at:
    {ProgramData}/FryNetworks/miner-BM/config/tos_state.json

It records whether the user has accepted the Mysterium Network Terms &
Conditions, when, and through which UI path (installer, GUI catch-up, etc.).
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TOS_VERSION = "2026-04-26"

_ACCEPTED_VIA_VALUES = frozenset({
    "installer-interactive",
    "installer-declined",
    "installer-quiet-deferred",
    "gui-catchup",
    "gui-catchup-declined",
    "gui-existing-tequilapi",
    "gui-toggle",
    "gui-toggle-declined",
})


def read_tos_state(config_dir: Path) -> Optional[dict]:
    """Read and parse tos_state.json from *config_dir*.

    Returns the parsed dict, or ``None`` if the file is missing, empty,
    or contains malformed JSON.
    """
    tos_file = config_dir / "tos_state.json"
    try:
        if not tos_file.exists():
            return None
        text = tos_file.read_text(encoding="utf-8")
        if not text.strip():
            return None
        data = json.loads(text)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:
        return None


def write_tos_state(
    config_dir: Path,
    accepted_via: str,
    tos_pending_catchup: bool = False,
) -> bool:
    """Write (or overwrite) tos_state.json in *config_dir*.

    Returns ``True`` on success, ``False`` on any write error.
    """
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        tos_file = config_dir / "tos_state.json"
        data = {
            "tos_version": TOS_VERSION,
            "accepted_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "accepted_via": accepted_via,
            "tos_pending_catchup": tos_pending_catchup,
        }
        tos_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def is_resolved_accept(tos: Optional[dict]) -> bool:
    """Return ``True`` if *tos* represents a resolved (non-declined, non-pending) acceptance."""
    if tos is None:
        return False
    via = tos.get("accepted_via", "")
    if via.endswith("-declined"):
        return False
    if tos.get("tos_pending_catchup"):
        return False
    return True
