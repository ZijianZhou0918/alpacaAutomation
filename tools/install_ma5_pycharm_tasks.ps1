$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$WatchcodeScript = Join-Path $ProjectDir "tools\start_ma5_watchcode_pycharm_gui.ps1"
$MonitorScript = Join-Path $ProjectDir "tools\start_ma5_monitor_pycharm_gui.ps1"
$HealthCheckScript = Join-Path $ProjectDir "tools\check_ma5_0400_health.ps1"
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

foreach ($deprecatedTaskName in @("AlpacaMA5-2350-EnsureMonitor-PyCharm")) {
    $deprecatedTask = Get-ScheduledTask -TaskName $deprecatedTaskName -ErrorAction SilentlyContinue
    if ($deprecatedTask) {
        Unregister-ScheduledTask -TaskName $deprecatedTaskName -Confirm:$false
        Write-InstallLog "Removed deprecated task $deprecatedTaskName."
    }
}

Register-Ma5Task `
    -Name "AlpacaMA5-2200-GenerateWatchcode-PyCharm" `
    -ScriptPath $WatchcodeScript `
    -At "22:00" `
    -Description "At 22:00, generate intraday and premarket watchcode files only when tomorrow is a US equity trading day; open PyCharm if possible, then use direct python fallback."

Register-Ma5Task `
    -Name "AlpacaMA5-0050-EnsureMonitor-PyCharm" `
    -ScriptPath $MonitorScript `
    -At "00:50" `
    -Description "At 00:50, start monitor_auto.py only when today is a US equity trading day; the single entrypoint prepares watchcodes and runs the active session monitor."

Register-Ma5Task `
    -Name "AlpacaMA5-0400-HealthCheck-PyCharm" `
    -ScriptPath $HealthCheckScript `
    -At "04:00" `
    -Description "At 04:00, on US equity trading days only, ensure monitor_auto.py is running and regenerate stale watchcode files."

Write-InstallLog "Done. Monitor task opens one visible monitor_auto.py Python window; output is also written to outputs\\logs."
