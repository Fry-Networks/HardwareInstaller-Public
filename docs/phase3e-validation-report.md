# Phase 3e — Hub Lifecycle E2E Validation Report

## Part 1 (initial attempt — encountered splash bug)

### Timeline
| Step | Time (CDT) | Result |
|------|-----------|--------|
| Step 0: State backup | 2026-05-01 00:08 | PASS — baseline captured |
| Step a: op signin | 00:09 | PASS — 45 vaults |
| Step b.1: Build 4.0.16 | 00:12 | PASS — FryHubSetup-4.0.16.exe 161,543,916 bytes |
| Step b.2: Save artifacts | 00:13 | PASS — SHA256 AAAE7308...matched |
| Step c: Upload 4.0.16 to CDN | 00:25 | PASS — archive+latest+manifest all HTTP 200 |
| GATE 1 | 00:25 | PASS |
| Step e: Inno install 4.0.16 | 00:26 | PASS — exit 0, 9 seconds |
| GATE 2 | 00:26 | PASS — HKLM Version=4.0.16, task present, BM intact (12 items) |
| Step f: Launch Hub, verify 4.0.16 | 00:28 | PASS — 4.0.16 shown, no update modal |
| Step g: Bump to 4.0.17 | 00:30 | PASS |
| Step h: Build 4.0.17 | 00:35 | PASS — FryHubSetup-4.0.17.exe 161,542,217 bytes |
| GATE 3 | 00:40 | PASS |
| Step i: Upload 4.0.17 to CDN | 00:41 | PASS — manifest hub_version=4.0.17 |
| GATE 4 | 00:42 | PASS |
| Step j: Launch Hub for update | 02:50 | **FAIL** — Hub stuck on splash screen |
| Step k: Auto-update | - | **BLOCKED** — modal hidden behind splash overlay |

### Root cause
PyInstaller splash overlay (`pyi_splash`) hides QMessageBox at installer_main.py:284.
Call chain: L649 splash show → L717 QApplication → L747 `_attempt_hub_update_check` →
L284 `dlg.exec()` blocks in modal event loop → L780 `window.show()` never reached →
L785 `pyi_splash.close()` never reached.

Hub PID 48212 hung indefinitely. Killed via elevated taskkill.

### Bunny CDN workarounds discovered
1. `storage.bunnycdn.com` resolves to 127.76.0.x loopback — use `ny.storage.bunnycdn.com`
2. Bunny Storage API rejects HEAD method — patched `_head` to use GET+Range
3. Storage zone password (41 chars) differs from Account API Key (72 chars) — fetch
   zone password at runtime via `api.bunny.net/storagezone`
4. `_purge` needs Account API Key, not storage zone password — purge separately

---

## Part 2 (splash fix + re-validation)

### Splash fix
- Commit: `c4d2860` — `installer: close pyi_splash before modal dialogs`
- Diff: +11 lines pure addition after `_slog.info("QApplication created")` (L719)
- Pattern mirrored from existing close at L793-798 (try/except ImportError/pass)
- Existing close at L793-798 remains as idempotent safety net

### Timeline
| Step | Time (CDT) | Result |
|------|-----------|--------|
| Step 0: Part 2 setup | 03:22 | PASS — resumed backup, renamed broken artifacts |
| Steps 1-3: Read patterns, backup, apply fix | 03:23 | PASS — 3 pyi_splash sites, clean diff |
| Step 4: Commit 1 (splash fix) | 03:24 | PASS — c4d2860 |
| Step 5: Build 4.0.16-splashfix | 03:25 | PASS — SHA256 DE530813... |
| Step 6: Save artifact | 03:30 | PASS — double-saved |
| Steps 7-8: Bump to 4.0.18, build | 03:32 | PASS — FryHubSetup-4.0.18.exe SHA256 1E551DEB... |
| GATE 7 | 03:33 | PASS — both builds present, backup intact |
| Step 10: Upload 4.0.18 to CDN | 03:34 | PASS — manifest hub_version=4.0.18 |
| GATE 8 | 03:34 | PASS — all archives (4.0.16, 4.0.17, 4.0.18) HTTP 200 |
| Step 11: Uninstall broken 4.0.16 | 03:34 | PASS (elevated cleanup needed for reg+files) |
| Step 12: Install 4.0.16-splashfix | 03:35 | PASS — exit 0, Version=4.0.16 |
| GATE 10 | 03:35 | PASS |
| **Step 13: SPLASH FIX VALIDATION** | 03:35 | **PASS — modal appeared VISIBLY, no hang** |
| Step 14: Auto-update to 4.0.18 | 03:36 | PARTIAL — Inno logged success but file not replaced |
| Step 14b: Manual install 4.0.18 | 03:47 | PASS — SHA256 16B01A5C... matches |
| GATE 11 | 03:47 | PASS (manual install path) |
| Step 15: CDN rollback validation | 03:48 | PASS — archive exists, manifest=4.0.18 |
| Step 16: Manual downgrade to 4.0.16-splashfix | 03:48 | PASS — Version=4.0.16, BM intact |
| GATE 12 | 03:49 | PASS |
| Step 17: Hub post-downgrade | 03:49 | PASS — no hang, modal visible, dismissed |
| Step 18: Restore to 4.0.18 | 03:50 | PASS — Version=4.0.18, BM intact |

---

## Findings

### 1. Splash overlay hides modal dialogs (FIXED)
**Severity:** Critical (Hub hangs indefinitely on update detection)
**Root cause:** `pyi_splash.close()` called at L793 (after `window.show()`) but modal
dialogs at L284/L612 fire before that point.
**Fix:** Commit c4d2860 — close splash immediately after QApplication creation.
**Status:** Fixed and validated in Part 2.

### 2. _self_downgrade_check DisplayName filter mismatch
**Severity:** Low (currently beneficial — allows rollback)
**Detail:** fryhub.iss AppName="Fry Hub" → DisplayName="Fry Hub". The check at
installer_main.py:558 matches `"frynetworks" in dn.lower() and "installer" in dn.lower()`.
"fry hub" does not contain "frynetworks" → check returns None → allows launch.
**Phase 3g+ tech debt:** Add "fry hub" to filter OR check by AppId GUID
`{B8E3F1A2-7C4D-4E9B-9F1A-3D5C8E2F1B7A}_is1`.
**Effort:** 1-2 hours.

### 3. Forward-roll gap
**Detail:** `rollback_hub()` exists but no `forward_roll_hub()`. CDN rollback validated
read-only (archive HEAD check). Live rollback PUT skipped to preserve CDN end state at 4.0.18.
**Phase 3g+ proposal:** Add `forward_roll_hub()` that rewrites manifest to point at
`/hub/archive/FryHubSetup-{version}.exe` with sha256 verification.
**Effort:** 3-4 hours.

### 4. --upload-hub gating on prior run_build()
**Workaround:** Direct python invocation of `upload_hub()`.
**Phase 3g+ proposal:** Add `--upload-hub-only <path>` flag.
**Effort:** 1-2 hours.

### 5. Rollback-target known-good flag missing
**Detail:** After Part 2, `/hub/archive/` contains FryHubSetup-4.0.16.exe (broken),
FryHubSetup-4.0.17.exe (broken), FryHubSetup-4.0.18.exe (good). `rollback_hub()` would
happily point users at a broken predecessor.
**Phase 3g+ proposal:** Tag manifests with `known_good` attribute. `rollback_hub()` refuses
non-known-good targets.
**Effort:** 2-3 hours.

### 6. Auto-update Inno file replacement failure (NEW)
**Detail:** Hub auto-update mechanism works (detects update, shows modal, downloads Inno,
launches Inno). Inno log shows "Installation process succeeded" and "Successfully installed
the file". But the actual file on disk was NOT replaced — hash and timestamp unchanged.
Suspected: `subprocess.DETACHED_PROCESS` + file virtualization or permission elevation gap
between parent Hub (elevated via uac_admin) and child Inno process.
**Mitigation:** Manual Inno install via `Start-Process -Wait` works correctly.
**Phase 3g+ proposal:** Investigate DETACHED_PROCESS + elevation inheritance. Consider using
`creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` or launching via COM elevation.
**Effort:** 4-6 hours investigation + fix.

### 7. Bunny CDN API workarounds (runtime monkey-patches)
**Detail:** Three runtime patches needed for every upload: NY region endpoint, GET+Range
for HEAD, storage zone password via Account API.
**Phase 3g+ proposal:** Bake patches into bunny_upload.py source code. Add region
auto-detection, storage zone password caching, and dual-key auth (storage pw for PUT,
account key for purge).
**Effort:** 3-4 hours.

---

## CDN final state
- Manifest: hub_version=4.0.18 (good)
- Archive: FryHubSetup-4.0.16.exe (broken, forensic), FryHubSetup-4.0.17.exe (broken,
  forensic), FryHubSetup-4.0.18.exe (good)
- DO NOT delete forensic broken artifacts — they document the splash bug.

## FryStation final state
- Hub installed: 4.0.18
- HKLM\Software\FryNetworks\Version: 4.0.18
- Add/Remove "Fry Hub" DisplayVersion: 4.0.18
- FryNetworksUpdater scheduled task: present
- BM miner: intact (14 items at C:\ProgramData\FryNetworks\miner-BM)
- AppId GUID: {B8E3F1A2-7C4D-4E9B-9F1A-3D5C8E2F1B7A}
- Backup directory: D:\Fry Networks\testing\work\phase3e-state.20260501000846\
  - Part 1 evidence: registry exports, Inno logs, SHA files, process captures
  - Part 2 evidence: part2.20260501032242\ — splashfix backup, build hashes, Inno logs

## Follow-up issues (Phase 3g+ tech debt)
1. Add "fry hub" to _self_downgrade_check filter (1-2h)
2. Add forward_roll_hub() to bunny_upload.py (3-4h)
3. Add --upload-hub-only flag to build_cli.py (1-2h)
4. Tag manifests with known_good attribute (2-3h)
5. Investigate auto-update DETACHED_PROCESS file replacement failure (4-6h)
6. Bake Bunny CDN workarounds into bunny_upload.py source (3-4h)
7. Phase 3f: deferred manual UI flows (tray toggle, settings, Inno desktopicon, uninstall E2E)
