"""
FryNetworks Config Backfill

Adds missing miner_code and poc_version fields to existing
installer_config.json files so the updater's discover_poc_installs()
can find and auto-update PoC services.

Usage:
    python config_backfill.py               # dry-run (default): print intended changes
    python config_backfill.py --commit      # write changes to disk
    python config_backfill.py --base-dir X  # override ProgramData path
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Fields written when creating a minimal config for a miner dir that has
# a PoC EXE but no installer_config.json.
MINIMAL_CONFIG_FIELDS = ["miner_code", "poc_version", "created_at"]

DEFAULT_BASE_DIR = r"C:\ProgramData\FryNetworks"


def _extract_miner_code_from_dir(miner_dir: Path) -> Optional[str]:
    """Derive miner_code from directory name: miner-BM -> 'BM'."""
    name = miner_dir.name
    if name.startswith("miner-"):
        code = name[len("miner-"):]
        # Ignore backup dirs like miner-BM.preFix3d.1777516901
        if "." in code:
            return None
        return code if code else None
    return None


def _parse_version_from_filename(filename: str) -> Optional[str]:
    """Extract version string from FRY_PoC_{CODE}_v*.exe filename."""
    match = re.match(r"FRY_PoC_\w+_v(.+)\.exe$", filename, re.IGNORECASE)
    if match:
        return match.group(1)
    return None


def _compare_semver_strings(a: str, b: str) -> int:
    """Compare two version strings. Returns -1/0/+1. Non-numeric parts sort as 0."""
    def to_tuple(s: str):
        base = s.split("-", 1)[0].split("+", 1)[0]
        parts = []
        for p in base.split("."):
            try:
                parts.append(int(p))
            except ValueError:
                parts.append(0)
        return tuple(parts)
    ta, tb = to_tuple(a), to_tuple(b)
    if ta < tb:
        return -1
    if ta > tb:
        return 1
    return 0


def _discover_poc_version(miner_dir: Path, miner_code: str) -> Optional[str]:
    """Find PoC version from FRY_PoC_{CODE}_v*.exe files in miner dir.

    If multiple matches, pick highest semver. If filename doesn't match
    regex, return None and log INCONCLUSIVE.
    """
    pattern = f"FRY_PoC_{miner_code}_v*.exe"
    candidates = list(miner_dir.glob(pattern))

    if not candidates:
        return None

    versions: list[tuple[str, str]] = []  # (version_string, filename)
    for c in candidates:
        # Skip .bak files
        if ".bak" in c.name.lower():
            continue
        ver = _parse_version_from_filename(c.name)
        if ver:
            versions.append((ver, c.name))
        else:
            print(f"  INCONCLUSIVE: {c.name} does not match version regex", file=sys.stderr)

    if not versions:
        return None

    if len(versions) > 1:
        print(f"  Multiple PoC EXEs found: {[v[1] for v in versions]}", file=sys.stderr)
        # Sort by semver, pick highest
        versions.sort(key=lambda x: x[0], reverse=True)
        # Use compare to be safe
        best = versions[0]
        for v in versions[1:]:
            if _compare_semver_strings(v[0], best[0]) > 0:
                best = v
        print(f"  Selected highest: {best[1]} (version {best[0]})", file=sys.stderr)
        return best[0]

    return versions[0][0]


def backfill_poc_discovery_fields(
    base_dir: Optional[str] = None,
    dry_run: bool = True,
) -> dict:
    """Backfill miner_code and poc_version into installer_config.json files.

    Args:
        base_dir: Parent directory containing miner-* subdirs.
        dry_run: If True, only print intended changes. If False, write to disk.

    Returns:
        Dict with keys: updated (list of paths), created (list), skipped (list), errors (list).
    """
    base = Path(base_dir or DEFAULT_BASE_DIR)
    result = {"updated": [], "created": [], "skipped": [], "errors": []}

    if not base.exists():
        result["errors"].append(f"Base dir does not exist: {base}")
        return result

    for miner_dir in sorted(base.iterdir()):
        if not miner_dir.is_dir():
            continue

        miner_code = _extract_miner_code_from_dir(miner_dir)
        if not miner_code:
            continue

        config_dir = miner_dir / "config"
        cfg_path = config_dir / "installer_config.json"

        # Discover PoC version from EXE filenames
        poc_version = _discover_poc_version(miner_dir, miner_code)

        if cfg_path.exists():
            # Load existing config
            try:
                existing = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, ValueError) as e:
                result["errors"].append(f"Malformed JSON at {cfg_path}: {e}")
                continue

            # Check if fields already present
            needs_update = False
            changes = {}

            if "miner_code" not in existing:
                changes["miner_code"] = miner_code
                needs_update = True
            if "poc_version" not in existing and poc_version:
                changes["poc_version"] = poc_version
                needs_update = True

            if not needs_update:
                result["skipped"].append(str(cfg_path))
                continue

            if dry_run:
                print(f"[DRY-RUN] Would add to {cfg_path}: {changes}")
                result["updated"].append(str(cfg_path))
            else:
                merged = existing.copy()
                merged.update(changes)
                try:
                    tmp_path = cfg_path.with_suffix(".json.tmp")
                    tmp_path.write_text(
                        json.dumps(merged, indent=2) + "\n", encoding="utf-8"
                    )
                    os.replace(str(tmp_path), str(cfg_path))
                    print(f"[COMMIT] Updated {cfg_path}: added {changes}")
                    result["updated"].append(str(cfg_path))
                except Exception as e:
                    result["errors"].append(f"Write failed for {cfg_path}: {e}")

        else:
            # No config exists — create minimal if we have enough info
            if not poc_version:
                print(
                    f"  Skipping {miner_dir.name}: no config and no PoC EXE version discoverable",
                    file=sys.stderr,
                )
                result["skipped"].append(str(cfg_path))
                continue

            minimal = {
                "miner_code": miner_code,
                "poc_version": poc_version,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }

            if dry_run:
                print(f"[DRY-RUN] Would create {cfg_path} with: {minimal}")
                result["created"].append(str(cfg_path))
            else:
                try:
                    config_dir.mkdir(parents=True, exist_ok=True)
                    tmp_path = cfg_path.with_suffix(".json.tmp")
                    tmp_path.write_text(
                        json.dumps(minimal, indent=2) + "\n", encoding="utf-8"
                    )
                    os.replace(str(tmp_path), str(cfg_path))
                    print(f"[COMMIT] Created {cfg_path}: {minimal}")
                    result["created"].append(str(cfg_path))
                except Exception as e:
                    result["errors"].append(f"Create failed for {cfg_path}: {e}")

    total_changed = len(result["updated"]) + len(result["created"])
    mode = "DRY-RUN" if dry_run else "COMMIT"
    print(f"\n[{mode}] CHANGED: {total_changed} files")
    if result["errors"]:
        print(f"[{mode}] ERRORS: {len(result['errors'])}", file=sys.stderr)
        for err in result["errors"]:
            print(f"  {err}", file=sys.stderr)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill miner_code/poc_version into installer_config.json files."
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write changes (default is dry-run).",
    )
    parser.add_argument(
        "--base-dir", default=DEFAULT_BASE_DIR,
        help=f"Parent dir containing miner-* subdirs (default: {DEFAULT_BASE_DIR})",
    )
    args = parser.parse_args()

    result = backfill_poc_discovery_fields(
        base_dir=args.base_dir,
        dry_run=not args.commit,
    )

    if result["errors"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
