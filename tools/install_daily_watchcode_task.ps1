[CmdletBinding()]
param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$TaskName = "AlpacaMA5-0050-GenerateWatchcodes"
$LegacyTaskNames = @("AlpacaMA5-0005-GenerateWatchcodes")
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Runner = Join-Path $PSScriptRoot "run_daily_watchcodes.ps1"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Daily WatchCode runner not found: $Runner"
}
if (-not (Test-Path -LiteralPath $PowerShellExe)) {
    throw "Windows PowerShell not found: $PowerShellExe"
}

$Arguments = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`""
$Action = New-ScheduledTaskAction `
    -Execute $PowerShellExe `
    -Argument $Arguments `
    -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At "00:50"
$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$Principal = New-ScheduledTaskPrincipal `
    -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive `
    -RunLevel Limited
$Description = "Generate intraday WatchCode daily at 00:50 local time. Premarket monitors positions only and has no stock screening."

if ($ValidateOnly) {
    Write-Output "TaskName=$TaskName"
    Write-Output "Schedule=Daily 00:50 local"
    Write-Output "Runner=$Runner"
    Write-Output "ValidationOnly=True"
    exit 0
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description $Description `
    -Force | Out-Null

foreach ($LegacyTaskName in $LegacyTaskNames) {
    $LegacyTask = Get-ScheduledTask -TaskName $LegacyTaskName -ErrorAction SilentlyContinue
    if ($null -ne $LegacyTask) {
        Unregister-ScheduledTask -TaskName $LegacyTaskName -Confirm:$false
        Write-Output "RemovedLegacyTask=$LegacyTaskName"
    }
}

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
$TaskInfo = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
Write-Output "TaskName=$($Task.TaskName)"
Write-Output "State=$($Task.State)"
Write-Output "NextRunTime=$($TaskInfo.NextRunTime.ToString('o'))"
Write-Output "Action=$($Task.Actions[0].Execute)"
Write-Output "Arguments=$($Task.Actions[0].Arguments)"
