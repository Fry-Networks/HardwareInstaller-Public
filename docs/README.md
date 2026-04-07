# FryNetworks Installer

## Environment Configuration

Create a `.env` file from the example:

```bash
cp .env.example .env
```

### Configuration Variables

The build script requires the following variables in `.env`:

- `OP_BEARER_TOKEN_REF`: 1Password reference for API bearer token
- `OP_GITHUB_REPO_REF`: 1Password reference for GitHub repository path
- `OP_GITHUB_TOKEN_REF`: 1Password reference for GitHub personal access token
- `EXTERNAL_API_BASE_URL`: External API endpoint (default: https://hardwareapi.frynetworks.com)

### Build Process

The build script (`build_installer.ps1`) will:
1. Load configuration from `.env` file
2. Retrieve secrets from 1Password using the configured references
3. Download required assets from GitHub
4. Create the bundled installer with embedded configuration

## Installation

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure environment:
```bash
cp .env.example .env
# Edit .env with your API settings
```

## Usage

### CLI Commands
```bash
# Validate a miner key
python installer_main.py validate --key BM-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456

# Install a miner
python installer_main.py install --key BM-ABC... --with-deps --auto-start

# Launch GUI
python installer_main.py --gui
```

## Bandwidth Sharing Tools (BM) and fVPN Rewards

Bandwidth Miner (BM) rewards are earned in **fVPN tokens**. By default, BM miners earn at a **0.25x base reward rate**. To unlock the full reward potential, users can enable bandwidth sharing tools within the **miner GUI** (not the installer). Each tool activated adds **+0.25x** to the multiplier:

- **No sharing tools enabled**: 0.25x rewards (base rate)
- **1 tool enabled**: 0.50x rewards
- **2 tools enabled**: 0.75x rewards  
- **3 tools enabled**: 1.00x rewards (full rate)

### Available Bandwidth Sharing Tools

The public build supports Mysterium as the sole bandwidth sharing partner:

- **Mysterium**: bandwidth sharing via Mysterium VPN SDK (mandatory for BM)

### What the installer does

The installer prepares everything needed for the GUI to manage sharing tools:

- **Stage Mysterium SDK files**: For BM installations, Mysterium SDK assets are copied to `<install>/SDK/...`.
- **Create config files**: Mysterium config is created under `<install>/config/mysterium.json`.
- **Enable Mysterium by default**: An encrypted `config/sdk_config.enc` is created with Mysterium set to `true`.
- **Store credentials**: Mysterium credentials are collected/managed by the GUI at runtime.

### What the user sees

- **In the installer**: No consent dialogs or sharing tool toggles. The installer completes BM installation with all SDKs staged but disabled.
- **In the miner GUI**: Dedicated tabs for each sharing tool with:
	- Clear explanation of the reward multiplier system
	- Toggle buttons to enable/disable each tool
	- Consent collection and terms/privacy links managed by the GUI
	- Real-time status of activated tools

### How activation works

1. User installs BM via installer → all SDKs staged, all disabled
2. User opens miner GUI → sees current multiplier (0.25x base)
3. User navigates to a sharing tool tab → reviews info, clicks toggle
4. GUI handles consent, updates config, signals service to start SDK
5. Multiplier increases (+0.25x per tool enabled)

### Where files go

- Installation root: `C:\ProgramData\FryNetworks\miner-BM` (Windows) or `/var/lib/frynetworks/miner-BM` (Linux)
- SDK assets: `<install>/SDK/windows-myst-sdk` (or linux variants)
- Program configs: `<install>/config/*.json` and `config/sdk_config.enc`
- Logs: `<install>/logs/<program>/`

### Opt-out / removal

- **Disable in GUI**: Toggle off any tool in the miner GUI to stop it and reduce the multiplier.
- **Uninstall**: Uninstalling the miner removes all SDK binaries and configs; log folders may remain for troubleshooting.

### Build-time prerequisites (maintainers)

- **Mysterium**: No build-time secrets required; GUI manages runtime credentials


## Building the bundled installer (notes for maintainers)

This repository contains a PowerShell build script `build_installer.ps1` that prepares a `build_config.json` and produces a single-file Windows installer via PyInstaller.

Important points:

- The installer does NOT embed the miner GUI or service executables by default (this repository may have many GUI/PoC binaries). At runtime the installer downloads the appropriate release assets from GitHub.
- If your release assets are in a private GitHub repository, the runtime installer needs access to a GitHub token (PAT) in order to download them. To avoid prompting end users for your credentials, the build script embeds a `build_config.json` into the bundled EXE containing the API bearer token and GitHub token. This allows the packaged installer to perform authenticated downloads at runtime.
- The build script reads secrets from 1Password (via the `op` CLI) at build time. It expects the following 1Password items to exist when you run the build locally or in CI:
	- `op://VPS/Hardware_API/API_BEARER_TOKEN` — bearer token for external API (embedded in build_config.json)
	- `op://VSCode/hardware_exe/Github_repo_test` — GitHub owner/repo path (owner/repo)
	- `op://VSCode/hardware_exe/Github_token` — GitHub PAT used for authenticated runtime downloads

Security guidance:

- Embedding a GitHub PAT in the installer is a convenience to allow end-user installs without any manual configuration. However, embedding credentials increases the attack surface. Prefer to use a scoped, short-lived, least-privilege PAT that only has read access to private release assets. Rotate the token regularly.
- Alternatively, make the release assets public or host them behind a controlled proxy (CI artifact server) so no token is required at runtime.

How to build:

1. Authenticate with 1Password on the build machine (install the `op` CLI and sign in with your account used to store the secrets).
2. Run the PowerShell build script from the repository root (Windows PowerShell / PowerShell 7+):

```powershell
.\build_installer.ps1 -Version "1.2.3"
```

The script will create a `build_config.json`, embed it into the PyInstaller bundle, build `dist\frynetworks_installer.exe`, and clean up the temporary `build_config.json` file.

If you prefer not to embed a PAT in the installer, consider one of these approaches:

- Build the installer in a trusted CI/CD environment that retrieves private assets at build time and embeds the assets directly (not recommended for many binaries due to size).
- Host private assets in a secure artifact repository and make the installer download them from that repository using short-lived credentials or a signed URL.
- Make the GitHub release assets public.

If you'd like, I can add a CI-friendly variant of `build_installer.ps1` that accepts the GitHub owner/repo and token via secure CI environment variables instead of 1Password, and documents rotating the token.
