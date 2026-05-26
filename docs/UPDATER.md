# FryNetworks Installer Updater (Bunny CDN + scheduled task)

The updater checks the Bunny CDN manifest for a newer installer version, downloads it
with SHA256 verification, and launches the new installer. PoC service binaries are
updated from GitHub Releases. A scheduled task runs it at logon and daily at 2 AM.

## Files
- `tools/updater.py` — Fetches manifest from Bunny CDN, downloads installer, verifies
  SHA256, runs installer (blocking). Also handles PoC binary updates via GitHub Releases.
  Logs to `C:\ProgramData\FryNetworks\updater\updater.log`.
- `tools/config_backfill.py` — Backfills `miner_code` and `poc_version` into
  `installer_config.json` files for existing installs (idempotent).
- `tools/register_updater_task.ps1` — Registers/unregisters the scheduled task.

## Update channels
- **Installer self-update**: Bunny CDN manifest at
  `https://frynetworks-downloads.b-cdn.net/frynetworks-installer/latest/version.json`
- **PoC binary update**: GitHub Releases from `Fry-Foundation/HardwarePoC_releases`

## Version discovery
When `--current-version` is not passed (normal scheduled task operation), the updater
auto-discovers the installed version via cascade:
1. PE FileVersion of `frynetworks_installer*.exe` under `miner-*` dirs
2. MAX `installer_version` across all `miner-*/config/installer_config.json` files
3. If none found, exits with code 7

## Build the updater EXE
```powershell
pyinstaller --onefile --windowed --icon resources\frynetworks_logo.ico --name frynetworks_updater tools\updater.py
```
Or via the main build script (step 5a):
```powershell
.\build_installer.ps1
```

## Register the scheduled task
```powershell
powershell -ExecutionPolicy Bypass -File tools\register_updater_task.ps1
```
Options: `-RunNow` to trigger immediately, `-Remove` to delete the task.

## Configuration
- Manifest URL: defaults to production; override with `--manifest-url` for testing.
- PoC repo: defaults to `Fry-Foundation/HardwarePoC_releases`; override with `--poc-repo`.
- Auth: PoC repos may need `--token` or `GITHUB_TOKEN` env var. Installer updates via
  Bunny CDN need no authentication.
- Quiet: `--quiet` suppresses stdout (log file always written).
- Dry-run: `--dry-run` reports without downloading or installing.

## Exit codes
| Code | Meaning |
|------|---------|
| 0 | Success / no update needed |
| 2 | Manifest fetch failed |
| 3 | Manifest missing required fields |
| 4 | Download failed (partial file cleaned) |
| 5 | SHA256 mismatch (file deleted) |
| 6 | Installer execution failed |
| 7 | Version discovery failed |

## How it works
1. Backfill `miner_code`/`poc_version` into configs if missing (idempotent).
2. Discover current installed version (PE → config → fail).
3. Fetch Bunny CDN manifest JSON.
4. Compare manifest `version` vs current — skip if not newer.
5. Download installer to temp, verify SHA256 from manifest.
6. Run installer (blocking, captures output to log).
7. If `--update-poc`: check GitHub releases for PoC binary updates.

## Uninstall/cleanup
- Remove scheduled task: `register_updater_task.ps1 -Remove`
- Delete `C:\ProgramData\FryNetworks\updater` to purge updater + logs.
