$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$MonitorScript = Join-Path $ProjectDir "tools\start_ma5_monitor_pycharm_gui.ps1"
$WatchcodeScript = Join-Path $ProjectDir "tools\start_ma5_watchcode_pycharm_gui.ps1"
$WatchCodesFile = Join-Path $ProjectDir "data\watchcodes\watch_codes.txt"
$PremarketWatchCodesFile = Join-Path $ProjectDir "data\watchcodes\watch_codes_premarket.txt"
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
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        return $null
    }

    foreach ($line in Get-Content -Path $Path -TotalCount 10 -ErrorAction Stop) {
        if ($line -match '^#\s*signal_date=(\d{4}-\d{2}-\d{2})\s*$') {
            return [datetime]::ParseExact($Matches[1], "yyyy-MM-dd", $null).Date
        }
    }
    return $null
}

function Get-LatestTradingSignalDate {
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

    $checker = Join-Path $ProjectDir "tools\check_ma5_trading_day.py"
    $python = Resolve-Ma5Python $ProjectDir
    $pythonArgs = @($python.Args + @($checker, "--latest-on-or-before", $candidate.ToString("yyyy-MM-dd")))
    $output = & $python.FilePath @pythonArgs 2>&1
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Could not resolve latest trading signal date (exit code $exitCode): $($output -join ' | ')"
    }

    $resolved = [string]($output | Select-Object -Last 1)
    return [datetime]::ParseExact($resolved.Trim(), "yyyy-MM-dd", $null).Date
}

function Test-WatchCodesFresh {
    param(
        [string]$Path,
        [string]$Label
    )

    if (-not (Test-Path $Path)) {
        Write-Ma5TaskLog $LogDir $LogFile "$Label is missing."
        return $false
    }

    $item = Get-Item $Path
    $ageHours = ((Get-Date) - $item.LastWriteTime).TotalHours
    $signalDate = Get-WatchCodesSignalDate $Path
    $expectedDate = Get-LatestTradingSignalDate
    $signalText = if ($signalDate) { $signalDate.ToString("yyyy-MM-dd") } else { "missing" }

    Write-Ma5TaskLog $LogDir $LogFile ("{0} LastWriteTime={1}; age={2:N2}h; signal_date={3}; expected_trading_signal={4}" -f $Label, $item.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss"), $ageHours, $signalText, $expectedDate.ToString("yyyy-MM-dd"))

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

    $autoMonitor = Get-Ma5PythonProcess $ProjectDir "monitor_auto.py"
    if ($autoMonitor) {
        $ids = ($autoMonitor | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "Auto monitor already running. PIDs: $ids"
    } else {
        Write-Ma5TaskLog $LogDir $LogFile "Auto monitor is missing; starting monitor task now."
        Invoke-Ma5ChildTask $MonitorScript "monitor"
    }

    $intradayFresh = Test-WatchCodesFresh $WatchCodesFile "watch_codes.txt"
    $premarketFresh = Test-WatchCodesFresh $PremarketWatchCodesFile "watch_codes_premarket.txt"
    if ($intradayFresh -and $premarketFresh) {
        Write-Ma5TaskLog $LogDir $LogFile "Both watchcode files are fresh; skip watchcode generation."
    } else {
        Write-Ma5TaskLog $LogDir $LogFile "At least one watchcode file is stale; regenerating intraday and premarket watchcodes now."
        Invoke-Ma5ChildTask $WatchcodeScript "watchcode"
    }

    Write-Ma5TaskLog $LogDir $LogFile "04:00 MA5 health check completed."
    exit 0
} catch {
    Write-Ma5TaskLog $LogDir $LogFile "ERROR: $($_.Exception.Message)"
    exit 1
}
