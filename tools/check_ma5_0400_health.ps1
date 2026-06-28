$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$MonitorScript = Join-Path $ProjectDir "tools\start_ma5_monitor_pycharm_gui.ps1"
$WatchcodeScript = Join-Path $ProjectDir "tools\start_ma5_watchcode_pycharm_gui.ps1"
$WatchCodesFile = Join-Path $ProjectDir "watch_codes.txt"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("ma5_0400_health_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$CommonScript = Join-Path $ProjectDir "tools\ma5_task_common.ps1"
$FreshHours = 18

if (-not (Test-Path $CommonScript)) {
    throw "Common task script not found: $CommonScript"
}
. $CommonScript

function Invoke-Ma5ChildTask {
    param(
        [string]$ScriptPath,
        [string]$Label
    )

    if (-not (Test-Path $ScriptPath)) {
        throw "$Label script not found: $ScriptPath"
    }

    Write-Ma5TaskLog $LogDir $LogFile "Starting $Label task: $ScriptPath"
    $argumentList = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`""
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $argumentList `
        -WorkingDirectory $ProjectDir `
        -WindowStyle Hidden `
        -Wait `
        -PassThru

    if ($process.ExitCode -ne 0) {
        throw "$Label task failed with exit code $($process.ExitCode)."
    }
    Write-Ma5TaskLog $LogDir $LogFile "$Label task completed."
}

function Get-WatchCodesSignalDate {
    if (-not (Test-Path $WatchCodesFile)) {
        return $null
    }

    foreach ($line in Get-Content -Path $WatchCodesFile -TotalCount 10 -ErrorAction Stop) {
        if ($line -match '^#\s*signal_date=(\d{4}-\d{2}-\d{2})\s*$') {
            return [datetime]::ParseExact($Matches[1], "yyyy-MM-dd", $null).Date
        }
    }
    return $null
}

function Get-LatestWeekdaySignalDate {
    try {
        $eastern = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
        $now = [System.TimeZoneInfo]::ConvertTime((Get-Date), $eastern)
    } catch {
        $now = Get-Date
    }

    if ($now.TimeOfDay -ge [TimeSpan]::Parse("16:15")) {
        $candidate = $now.Date
    } else {
        $candidate = $now.Date.AddDays(-1)
    }

    while ($candidate.DayOfWeek -in @([DayOfWeek]::Saturday, [DayOfWeek]::Sunday)) {
        $candidate = $candidate.AddDays(-1)
    }
    return $candidate
}

function Test-WatchCodesFresh {
    if (-not (Test-Path $WatchCodesFile)) {
        Write-Ma5TaskLog $LogDir $LogFile "watch_codes.txt is missing."
        return $false
    }

    $item = Get-Item $WatchCodesFile
    $ageHours = ((Get-Date) - $item.LastWriteTime).TotalHours
    $signalDate = Get-WatchCodesSignalDate
    $expectedDate = Get-LatestWeekdaySignalDate
    $signalText = if ($signalDate) { $signalDate.ToString("yyyy-MM-dd") } else { "missing" }

    Write-Ma5TaskLog $LogDir $LogFile ("watch_codes.txt LastWriteTime={0}; age={1:N2}h; signal_date={2}; expected_weekday_signal={3}" -f $item.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss"), $ageHours, $signalText, $expectedDate.ToString("yyyy-MM-dd"))

    if ($ageHours -le $FreshHours) {
        return $true
    }

    if (-not $signalDate) {
        return $false
    }
    return $signalDate.Date -eq $expectedDate.Date
}

try {
    Write-Ma5TaskLog $LogDir $LogFile "Starting 04:00 MA5 health check."
    if (-not (Test-Ma5TradingDayForTask $ProjectDir $LogDir $LogFile 0 "today 04:00 health check")) {
        exit 0
    }

    $monitor = Get-Ma5PythonProcess $ProjectDir "monitor_ma5_forever.py"
    if ($monitor) {
        $ids = ($monitor | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "Monitor already running. PIDs: $ids"
    } else {
        Write-Ma5TaskLog $LogDir $LogFile "Monitor is not running; starting it now."
        Invoke-Ma5ChildTask $MonitorScript "monitor"
    }

    if (Test-WatchCodesFresh) {
        Write-Ma5TaskLog $LogDir $LogFile "watch_codes.txt is fresh; skip watchcode generation."
    } else {
        Write-Ma5TaskLog $LogDir $LogFile "watch_codes.txt is stale; regenerating watchcode now."
        Invoke-Ma5ChildTask $WatchcodeScript "watchcode"
    }

    Write-Ma5TaskLog $LogDir $LogFile "04:00 MA5 health check completed."
    exit 0
} catch {
    Write-Ma5TaskLog $LogDir $LogFile "ERROR: $($_.Exception.Message)"
    exit 1
}
