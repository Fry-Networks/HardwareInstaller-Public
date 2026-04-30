param([string]$Version = "")
$ErrorActionPreference = "Stop"

# 1Password references for retrieving secrets at build time
$OP_BEARER_TOKEN_REF = "op://HardwareAPI/Hardware_API/API_BEARER_TOKEN"
$OP_GUI_GITHUB_REPO_REF = "op://VSCode/hardware_exe/Github_repo_hardware_exe"
$OP_GUI_GITHUB_TOKEN_REF = ""
$OP_HW_GITHUB_REPO_REF = "op://VSCode/hardware_exe/Github_repo_hardwareinstaller"
$OP_HW_GITHUB_TOKEN_REF = ""
$OP_POC_GITHUB_REPO_REF = "op://VSCode/hardware_exe/Github_repo_hardwarepoc"
$OP_POC_GITHUB_TOKEN_REF = ""
$EXTERNAL_API_BASE_URL = "https://hardwareapi.frynetworks.com"
$OLOSTEP_BROWSER_URL = "https://olostepbrowser.s3.us-east-1.amazonaws.com/setup.exe"
$OP_MYSTERIUM_PAYOUT_REF = "op://Bandwidth Miners/Mysterium SDK API/MYST_PAYOUT_ADDR"
$OP_MYSTERIUM_REG_TOKEN_REF = "op://Bandwidth Miners/Mysterium SDK API/MYST_REG_TOKEN"
$OP_MYSTERIUM_API_KEY_REF = "op://Bandwidth Miners/Mysterium SDK API/MYST_API_KEY"
# Encryption key references (create these in 1Password before first build)
$OP_ENC_SDK_SALT_REF = "op://Bandwidth Miners/Encryption Keys/SDK_SALT"
$OP_ENC_SDK_PASSWORD_REF = "op://Bandwidth Miners/Encryption Keys/SDK_PASSWORD"

# If no version provided, read from version.py
if ([string]::IsNullOrWhiteSpace($Version)) {
    Write-Host "No version specified, reading from version.py..." -ForegroundColor Gray
    $VersionFile = Join-Path $PSScriptRoot "version.py"
    if (Test-Path $VersionFile) {
        $VersionContent = Get-Content $VersionFile -Raw
        if ($VersionContent -match '__version__\s*=\s*["' + "'" + ']([^"' + "'" + ']+)["' + "'" + ']') {
            $Version = $matches[1]
            Write-Host "  [OK] Version from version.py: $Version" -ForegroundColor Green
        } else {
            Write-Host "  [FAIL] Could not parse version from version.py, using default 1.0.0" -ForegroundColor Yellow
            $Version = "1.0.0"
        }
    } else {
        Write-Host "  [FAIL] version.py not found, using default version 1.0.0" -ForegroundColor Yellow
        $Version = "1.0.0"
    }
}

Write-Host "========================================"  -ForegroundColor Cyan
Write-Host "Fry Hub Build Script" -ForegroundColor Cyan
Write-Host "Version: $Version" -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan
$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $InstallerDir
Write-Host "`n[1/5] Retrieving bearer token from 1Password..." -ForegroundColor Yellow
try {
    $BearerToken = op read $OP_BEARER_TOKEN_REF
    if ([string]::IsNullOrWhiteSpace($BearerToken)) { throw "Bearer token is empty" }
    Write-Host "  [OK] Bearer token retrieved successfully" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve bearer token from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}
Write-Host "`n[1b/5] Retrieving GUI GitHub repo path from 1Password..." -ForegroundColor Yellow
try {
    $GuiGithubPath = op read $OP_GUI_GITHUB_REPO_REF
    if ([string]::IsNullOrWhiteSpace($GuiGithubPath)) { throw "GUI GitHub path is empty" }
    Write-Host "  [OK] GUI GitHub path retrieved: $GuiGithubPath" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve GUI GitHub path from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1c/5] Retrieving PoC GitHub repo path from 1Password..." -ForegroundColor Yellow
try {
    $PocGithubPath = op read $OP_POC_GITHUB_REPO_REF
    if ([string]::IsNullOrWhiteSpace($PocGithubPath)) { throw "PoC GitHub path is empty" }
    Write-Host "  [OK] PoC GitHub path retrieved: $PocGithubPath" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve PoC GitHub path from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

# For build-time packaging: download the release assets (EXEs) and embed them in resources\embedded
Write-Host "`n[1d/5] Retrieving GUI GitHub PAT from 1Password..." -ForegroundColor Yellow
try {
    if ([string]::IsNullOrEmpty($OP_GUI_GITHUB_TOKEN_REF)) {
        Write-Warning "OP_GUI_GITHUB_TOKEN_REF empty — building with no GUI PAT (matches v4.0.9d shipped state)"
        $GuiGithubPAT = ""
    } else {
        $GuiGithubPAT = op read $OP_GUI_GITHUB_TOKEN_REF
        if ([string]::IsNullOrWhiteSpace($GuiGithubPAT)) { throw "GUI GitHub PAT is empty" }
        Write-Host "  [OK] GUI GitHub PAT retrieved for build (hidden)" -ForegroundColor Green
    }
} catch {
    Write-Host "  [FAIL] Failed to retrieve GUI GitHub PAT from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1e/5] Retrieving PoC GitHub PAT from 1Password..." -ForegroundColor Yellow
try {
    if ([string]::IsNullOrEmpty($OP_POC_GITHUB_TOKEN_REF)) {
        Write-Warning "OP_POC_GITHUB_TOKEN_REF empty — building with no PoC PAT (matches v4.0.9d shipped state)"
        $PocGithubPAT = ""
    } else {
        $PocGithubPAT = op read $OP_POC_GITHUB_TOKEN_REF
        if ([string]::IsNullOrWhiteSpace($PocGithubPAT)) { throw "PoC GitHub PAT is empty" }
        Write-Host "  [OK] PoC GitHub PAT retrieved for build (hidden)" -ForegroundColor Green
    }
} catch {
    Write-Host "  [FAIL] Failed to retrieve PoC GitHub PAT from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

# Mysterium credentials: required to be provided via 1Password for builds
if (-not $OP_MYSTERIUM_PAYOUT_REF -or -not $OP_MYSTERIUM_REG_TOKEN_REF -or -not $OP_MYSTERIUM_API_KEY_REF) {
    Write-Host "`n[1h/5] [FAIL] One or more OP_MYSTERIUM_* refs not set - Mysterium credentials must be provided via 1Password" -ForegroundColor Red
    exit 1
}

Write-Host "`n[1h/5] Retrieving Mysterium credentials from 1Password..." -ForegroundColor Yellow
try {
    $MysteriumPayout = (op read $OP_MYSTERIUM_PAYOUT_REF).Trim()
    $MysteriumReg = (op read $OP_MYSTERIUM_REG_TOKEN_REF).Trim()
    $MysteriumApiKey = (op read $OP_MYSTERIUM_API_KEY_REF).Trim()
    if ([string]::IsNullOrWhiteSpace($MysteriumPayout) -or [string]::IsNullOrWhiteSpace($MysteriumReg) -or [string]::IsNullOrWhiteSpace($MysteriumApiKey)) {
        throw "One or more Mysterium credentials are empty"
    }
    Write-Host "  [OK] Mysterium credentials embedded for BM installs" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve Mysterium credentials from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}



Write-Host "`n[1i/5] Retrieving encryption keys from 1Password..." -ForegroundColor Yellow
try {
    $EncSdkSalt = (op read $OP_ENC_SDK_SALT_REF).Trim()
    $EncSdkPassword = (op read $OP_ENC_SDK_PASSWORD_REF).Trim()
    if ([string]::IsNullOrWhiteSpace($EncSdkSalt) -or [string]::IsNullOrWhiteSpace($EncSdkPassword)) {
        throw "One or more encryption keys are empty"
    }
    Write-Host "  [OK] Encryption keys retrieved" -ForegroundColor Green
} catch {
    Write-Host "  [FAIL] Failed to retrieve encryption keys from 1Password" -ForegroundColor Red
    Write-Host "  Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[2/5] Creating build_config.json..." -ForegroundColor Yellow
$BuildConfig = @{ 
    external_api = @{ base_url = $EXTERNAL_API_BASE_URL; bearer_token = $BearerToken; timeout = 10.0 }; 
    github = @{
        gui = @{ path = $GuiGithubPath; token = $GuiGithubPAT };
        poc = @{ path = $PocGithubPath; token = $PocGithubPAT }
    };
    encryption = @{
        sdk = @{ salt = $EncSdkSalt; password = $EncSdkPassword }
    };
    partner_integrations = @{
        mystnodes_sdk = @{
            enabled = $true;
            payout_addr = $MysteriumPayout;
            reg_token = $MysteriumReg;
            api_key = $MysteriumApiKey
        }
    };
    status = "embedded";
    source = "1password"; 
    version = $Version; 
    build_date = (Get-Date -Format "yyyy-MM-dd HH:mm:ss") 
} | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText("$PWD\build_config.json", $BuildConfig, (New-Object System.Text.UTF8Encoding $false))
Write-Host "  [OK] build_config.json created" -ForegroundColor Green
Write-Host "`n[3/4] Preparing embedded resources..." -ForegroundColor Yellow

# Copy NSSM (required utility)
# Look for nssm.exe in the repository's tools/ directory (installer root)
$NssmSource = Join-Path $InstallerDir "tools\nssm.exe"
$NssmDest = Join-Path $InstallerDir "resources\embedded\nssm.exe"
if (Test-Path $NssmSource) {
    Copy-Item $NssmSource $NssmDest -Force
    Write-Host "  [OK] Copied NSSM ($(([math]::Round((Get-Item $NssmDest).Length / 1KB, 0))) KB)" -ForegroundColor Green
} else {
    Write-Host "  [FAIL] NSSM not found at $NssmSource - NSSM is required for building the installer." -ForegroundColor Red
    Write-Host "  Please place nssm.exe into the tools/ folder and re-run the build." -ForegroundColor Red
    exit 1
}

# NOTE: Service executables (miner binaries) are NOT embedded
# They should be obtained separately or downloaded during installation

Write-Host "`n[4/5] Cleaning previous builds..." -ForegroundColor Yellow
Remove-Item -Force -Recurse build,dist -ErrorAction SilentlyContinue
Write-Host "  [OK] Build directories cleaned" -ForegroundColor Green

# Build updater first so MSI bundling finds it in dist\
Write-Host "`n[5a/5] Building updater with PyInstaller..." -ForegroundColor Yellow
try {
    py -m PyInstaller `
        --onefile `
        --noconsole `
        --paths "." `
        --icon "resources\frynetworks_logo.ico" `
        --name frynetworks_updater `
        tools\updater.py
    if (-not (Test-Path "dist\frynetworks_updater.exe")) { throw "updater.exe not found after build" }
    Write-Host "  [OK] updater built: dist\frynetworks_updater.exe" -ForegroundColor Green
} catch {
    Write-Host "`n[FAIL] Updater build failed" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
}

Write-Host "`n[5b/5] Building installer with PyInstaller..." -ForegroundColor Yellow
Write-Host "  This may take 30-60 seconds..." -ForegroundColor Gray
$ExeName = "frynetworks_installer_v$Version"

# Note: Alternative build method using spec file (recommended to prevent duplicate tray icons):
# py -m PyInstaller frynetworks_installer.spec
# This method uses the spec file which includes Windows-specific settings

try {
    py -m PyInstaller `
        --onefile `
        --noconsole `
        --uac-admin `
        --paths "." `
        --paths ".\core" `
        --paths ".\gui" `
        --hidden-import "core.service_manager" `
        --hidden-import "core.config_manager" `
        --hidden-import "core.conflict_detector" `
        --hidden-import "core.naming" `
        --hidden-import "core.key_parser" `
        --collect-submodules "core" `
        --icon "resources\frynetworks_logo.ico" `
        --splash "resources\frynetworks_splash.png" `
        --add-data "build_config.json;." `
        --add-data "resources\background.png;resources" `
        --add-data "resources\frynetworks_logo.ico;resources" `
        --add-data "resources\embedded;resources\embedded" `
        --add-data "SDK;SDK" `
        --add-data "core;core" `
        --add-data "dist\frynetworks_updater.exe;." `
        --exclude-module numpy `
        --exclude-module PIL `
        --exclude-module Pillow `
        --exclude-module PySide6.QtQuick `
        --exclude-module PySide6.QtQml `
        --exclude-module PySide6.QtPdf `
        --exclude-module PySide6.QtWebEngineWidgets `
        --exclude-module PySide6.QtWebEngineCore `
        --name $ExeName `
        installer_main.py
    
    if (Test-Path "dist\$ExeName.exe") {
        Write-Host "`n[OK] Build completed successfully!" -ForegroundColor Green
        Write-Host "`nInstaller location:" -ForegroundColor Cyan
        Write-Host "  $(Join-Path $InstallerDir "dist\$ExeName.exe")" -ForegroundColor White
        $FileSize = (Get-Item "dist\$ExeName.exe").Length
        $FileSizeMB = [math]::Round($FileSize / 1MB, 2)
        Write-Host "`nFile size: $FileSizeMB MB" -ForegroundColor Gray
        Write-Host "`nTo test the installer:" -ForegroundColor Cyan
        Write-Host "  cd dist" -ForegroundColor White
        Write-Host "  .\$ExeName.exe --gui" -ForegroundColor White
    } else { throw "Build completed but executable not found" }
} catch {
    Write-Host "`n[FAIL] Build failed" -ForegroundColor Red
    Write-Host "Error: $_" -ForegroundColor Red
    exit 1
} finally {
    if (Test-Path "build_config.json") {
        Remove-Item "build_config.json" -Force
        Write-Host "`n  Cleaned up build_config.json" -ForegroundColor Gray
    }
    # Clean up embedded NSSM (it's now in the exe)
    if (Test-Path "resources\embedded\nssm.exe") {
        Remove-Item "resources\embedded\nssm.exe" -Force
    }
}
Write-Host "`n========================================"  -ForegroundColor Cyan
Write-Host "Build process complete!" -ForegroundColor Cyan
Write-Host "========================================"  -ForegroundColor Cyan

