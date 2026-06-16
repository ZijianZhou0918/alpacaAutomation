$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$WatchcodeScript = Join-Path $ProjectDir "tools\start_ma5_watchcode_pycharm_gui.ps1"
$MonitorScript = Join-Path $ProjectDir "tools\start_ma5_monitor_pycharm_gui.ps1"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$InstallLog = Join-Path $LogDir "ma5_pycharm_tasks_install.log"

function Write-InstallLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $InstallLog -Value $line -Encoding UTF8
    Write-Host $Message
}

function Register-Ma5Task {
    param(
        [string]$Name,
        [string]$ScriptPath,
        [string]$At,
        [string]$Description
    )

    if (-not (Test-Path $ScriptPath)) {
        throw "Task script not found: $ScriptPath"
    }

    $runAt = [datetime]::Today.Add([TimeSpan]::Parse($At))
    $action = New-ScheduledTaskAction `
        -Execute "powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`"" `
        -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -Daily -At $runAt
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4)
    $principal = New-ScheduledTaskPrincipal `
        -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive `
        -RunLevel Limited

    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Description $Description `
        -Force | Out-Null

    Write-InstallLog "Registered $Name at $At local time."
}

Register-Ma5Task `
    -Name "AlpacaMA5-2200-GenerateWatchcode-PyCharm" `
    -ScriptPath $WatchcodeScript `
    -At "22:00" `
    -Description "Open PyCharm if possible, then run watchcode_ma5.py through direct python fallback at 22:00."

Register-Ma5Task `
    -Name "AlpacaMA5-2350-EnsureMonitor-PyCharm" `
    -ScriptPath $MonitorScript `
    -At "23:50" `
    -Description "If monitor_ma5_forever.py is not running at 23:50, open PyCharm if possible and start it through direct python fallback."

Write-InstallLog "Done. These tasks open PyCharm when possible, then run through direct python fallback without keyboard shortcuts."
