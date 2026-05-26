# Fry Hub Plugin Manifest Schema

## Phase 1 — Local registry (`core/miner_registry.json`)

### Purpose

The local miner registry replaces the hardcoded `MINER_TYPES` dictionary that was
previously embedded in `core/key_parser.py`. Externalizing miner type definitions
into a standalone JSON file decouples miner metadata from installer logic, enabling
future CDN-hosted manifest delivery (Phase 2) and declarative install/uninstall
steps (Phase 3) without changing the loader contract.

### Schema (schema_version: 1)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Schema version number. Loader rejects unknown major versions. |
| `miners` | array | yes | Array of miner definition objects. |
| `miners[].code` | string | yes | Unique miner type code (e.g. `"BM"`, `"RDN"`). Used as key lookup. |
| `miners[].name` | string | yes | Human-readable display name (e.g. `"Bandwidth Miner"`). |
| `miners[].group` | string | yes | Miner group for conflict detection (e.g. `"Decibel"`, `"Satellite"`). |
| `miners[].exclusive` | string \| null | yes | Code of the mutually exclusive miner in the same hardware group, or `null`. |
| `miners[].requires_installer` | boolean | yes | Whether this miner type requires the Fry Hub installer binary. |
| `miners[].requires_stake` | boolean | yes | Whether this miner type requires an fNODE registration stake. |

### Example entry

```json
{
  "code": "IDM",
  "name": "Indoor Decibel Miner",
  "group": "Decibel",
  "exclusive": "ODM",
  "requires_installer": true,
  "requires_stake": false
}
```

### Loader behavior

`core/key_parser.py` loads the registry at module import time via `_load_miner_types()`.
The loader reconstructs the `MINER_TYPES` class attribute on `MinerKeyParser` with the
exact same shape as the former hardcoded dict:

```python
{
    "CODE": {"name": str, "group": str, "exclusive": str | None},
    ...
}
```

Fields added in Phase 1 that are not part of the original dict shape (`requires_installer`,
`requires_stake`) are intentionally excluded from the reconstructed dict to preserve
backward compatibility with all existing consumers.

---

## Phase 2 — CDN manifest (loader active, CDN upload pending)

### Purpose

Phase 2 hosts the miner registry on Bunny CDN alongside the existing installer
binary distribution. The manifest is fetched at installer startup (with a local
fallback to the embedded `core/miner_registry.json`), enabling over-the-air miner
type additions without rebuilding and redeploying the installer binary.

### Status

The loader (`core/registry_loader.py`) is active. Until the envelope JSON is
uploaded to the CDN URL below, every launch falls through CDN (404) → cache
(miss) → bundled JSON. This is correct, intended behavior.

### URL pattern

```
https://frynetworks-downloads.b-cdn.net/frynetworks-installer/manifest/v1/registry.json
```

The manifest is wrapped in an integrity envelope:

```json
{
  "manifest_version": "1.0.0",
  "sha256": "<hex digest of canonical JSON form of inner registry>",
  "registry": { ... }
}
```

SHA-256 is computed over the **canonical** JSON form:
`json.dumps(registry, sort_keys=True, separators=(",", ":"))`. This ensures
deterministic hashes regardless of whitespace or key ordering in the source file.

The loader verifies `sha256` before applying the registry, matching the existing
pattern used by `tools/updater.py` for installer binary updates.

### Loader architecture

Two-phase loading separates import-time (local) from startup (network):

1. **Import time** (`core/key_parser.py` → `registry_loader.load_local_registry()`):
   reads disk cache → bundled JSON. No network. Never raises.
2. **main() startup** (`installer_main.py` → `registry_loader.refresh_from_cdn()`):
   CDN fetch with 3s timeout → verify SHA-256 → write disk cache → update
   `MinerKeyParser.MINER_TYPES` in-place. On failure: no-op.

Worst-case latency: 3s (broken DNS / partial connectivity before timeout).
Confirmed-offline (adapter disabled) typically returns immediately.

### Cache location

```
C:\ProgramData\FryNetworks\cache\miner_registry.json
```

- Created on first successful CDN fetch (`mkdir -p` equivalent)
- Atomic write: `.json.tmp` + `os.replace` (matches `tools/updater.py:452-454`)
- `PermissionError` caught — non-elevated runs do not crash on cache write failure

### Fallback chain

```
CDN (3s timeout) → disk cache → bundled JSON
```

- **CDN**: freshest source, verified via SHA-256 envelope
- **Disk cache**: persists across sessions, survives crashes and updates
- **Bundled JSON**: built into PyInstaller onefile, always available

### Envelope generation

`build_cli.py:build_registry_envelope()` wraps `core/miner_registry.json` in the
integrity envelope and writes `dist/miner_registry_envelope.json` for manual CDN
upload.

### Additional fields planned for Phase 2

| Field | Type | Description |
|-------|------|-------------|
| `miners[].binary_url` | string | Bunny CDN URL for the miner service binary. |
| `miners[].binary_sha256` | string | SHA-256 hex digest of the binary, verified before install. |
| `miners[].binary_version` | string | Semantic version of the miner binary. |
| `miners[].min_installer_version` | string | Minimum Fry Hub version required to install this miner. |

---

## Phase 3+ — Declarative install/uninstall steps (planned)

### Purpose

Phase 3 replaces the procedural `ServiceManager` per-miner installation logic
with declarative step arrays in the manifest. This allows new miner types to be
fully defined in the registry without any Python code changes.

### Additional fields for Phase 3

| Field | Type | Description |
|-------|------|-------------|
| `miners[].install_steps` | array | Ordered list of install step objects. |
| `miners[].uninstall_steps` | array | Ordered list of uninstall step objects. |
| `miners[].dependencies` | array | List of dependency identifiers (e.g. `"nssm"`, `"ch341ser"`). |
| `miners[].firewall_rules` | array | Firewall port/protocol rules to create during install. |
| `miners[].service_config` | object | NSSM/systemd service parameters (name, args, restart policy). |

### Full field reference (all phases)

| Field | Phase | Type | Required | Description |
|-------|-------|------|----------|-------------|
| `schema_version` | 1 | integer | yes | Schema major version. |
| `miners[].code` | 1 | string | yes | Unique miner type code. |
| `miners[].name` | 1 | string | yes | Display name. |
| `miners[].group` | 1 | string | yes | Conflict group. |
| `miners[].exclusive` | 1 | string \| null | yes | Mutually exclusive code. |
| `miners[].requires_installer` | 1 | boolean | yes | Needs Fry Hub binary. |
| `miners[].requires_stake` | 1 | boolean | yes | Needs fNODE stake. |
| `miners[].binary_url` | 2 | string | no | CDN download URL. |
| `miners[].binary_sha256` | 2 | string | no | Binary integrity hash. |
| `miners[].binary_version` | 2 | string | no | Binary semver. |
| `miners[].min_installer_version` | 2 | string | no | Min Fry Hub version. |
| `miners[].install_steps` | 3 | array | no | Declarative install steps. |
| `miners[].uninstall_steps` | 3 | array | no | Declarative uninstall steps. |
| `miners[].dependencies` | 3 | array | no | Required system dependencies. |
| `miners[].firewall_rules` | 3 | array | no | Firewall rules to create. |
| `miners[].service_config` | 3 | object | no | Service manager parameters. |

---

## Backward compatibility

- **`schema_version`** gates loader behavior. The loader rejects registries with
  an unknown major version (currently only `1` is accepted).
- **Adding new optional fields** to miner entries is a non-breaking change.
  The loader ignores fields it does not consume.
- **Removing or renaming a required field** requires a `schema_version` bump and
  a corresponding loader update.
- **Phase 1 → Phase 2 migration**: same JSON shape, hosted on CDN with SHA-256
  integrity envelope. The local `core/miner_registry.json` becomes the offline
  fallback if CDN fetch fails.
- **Phase 2 → Phase 3 migration**: additive fields only (`install_steps`,
  `uninstall_steps`, etc.). No schema version bump required unless existing
  required fields change.
