# Registers or removes the FryNetworks updater scheduled task.
# The updater auto-discovers version from installed configs (no --current-version needed).
param(
    [string]$UpdaterPath = "C:\ProgramData\FryNetworks\updater\frynetworks_updater.exe",
    [string]$TaskName = "FryNetworksUpdater",
    [switch]$Remove = $false,
    [switch]$RunNow = $false,
    [string]$LogPath = "$env:TEMP\fry_updater_task.log"
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message)
    if ($LogPath) {
        $dir = Split-Path $LogPath -Parent
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        "$((Get-Date).ToString('s')) $Message" | Out-File -FilePath $LogPath -Append -Encoding utf8
    }
    Write-Host $Message
}

try {
    Write-Log "Starting updater task registration (TaskName=$TaskName)"

    if ($Remove) {
        $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Log "Removed task $TaskName"
        } else {
            Write-Log "Task $TaskName not found."
        }
        return
    }

    if (-not (Test-Path $UpdaterPath)) {
        throw "Updater not found at $UpdaterPath"
    }

    $action = New-ScheduledTaskAction -Execute $UpdaterPath -Argument "--quiet --update-poc" -WorkingDirectory (Split-Path $UpdaterPath)
    $triggers = @(
        New-ScheduledTaskTrigger -AtLogOn
        New-ScheduledTaskTrigger -Daily -At 10:00AM -RandomDelay (New-TimeSpan -Minutes 30)
    )
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Highest
    $taskPath = "\FryNetworks\"

    $existing = Get-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Log "Task $taskPath$TaskName already exists; skipping creation."
    } else {
        Register-ScheduledTask -TaskName $TaskName -TaskPath $taskPath -Action $action -Trigger $triggers -Description "Checks FryNetworks installer updates" -Settings $settings -Principal $principal -Force
        Write-Log "Registered task $taskPath$TaskName to run $UpdaterPath"
    }

    if ($RunNow) {
        Start-ScheduledTask -TaskPath $taskPath -TaskName $TaskName
        Write-Log "Started task $taskPath$TaskName"
    }
}
catch {
    Write-Log "Error: $_"
    throw
}
