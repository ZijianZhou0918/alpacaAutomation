$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$IntradayRunScript = Join-Path $ProjectDir "watchcode_ma5.py"
$PremarketRunScript = Join-Path $ProjectDir "watchcode_premarket.py"
$WatchCodesFile = Join-Path $ProjectDir "data\watchcodes\watch_codes.txt"
$PremarketWatchCodesFile = Join-Path $ProjectDir "data\watchcodes\watch_codes_premarket.txt"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("pycharm_watchcode_task_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$DirectRunLog = Join-Path $LogDir ("watchcode_direct_run_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$CommonScript = Join-Path $ProjectDir "tools\ma5_task_common.ps1"

if (-not (Test-Path $CommonScript)) {
    throw "Common task script not found: $CommonScript"
}
. $CommonScript

function Invoke-Ma5WatchcodeScript {
    param(
        [object]$Python,
        [string]$RunScript,
        [string]$ScriptName,
        [string]$OutputFile,
        [string]$Label,
        [datetime]$StartedAt
    )

    if (-not (Test-Path $RunScript)) {
        throw "$Label script not found: $RunScript"
    }

    $existing = Get-Ma5PythonProcess $ProjectDir $ScriptName
    if ($existing) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "$ScriptName is already running. Skip direct $Label generation. PIDs: $ids"
        return
    }

    Write-Ma5TaskLog $LogDir $LogFile "Running $ScriptName for $Label through direct python."
    Push-Location $ProjectDir
    try {
        $pythonExe = $Python.FilePath
        $pythonArgs = @($Python.Args + @($RunScript))
        & $pythonExe @pythonArgs *>> $DirectRunLog
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "Direct $ScriptName run failed with exit code $exitCode. See $DirectRunLog"
    }
    if (-not (Test-Path $OutputFile)) {
        throw "$ScriptName finished, but output file was not found: $OutputFile"
    }

    $watchCodes = Get-Item $OutputFile
    if ($watchCodes.LastWriteTime -lt $StartedAt.AddSeconds(-2)) {
        throw "$ScriptName finished, but output file was not updated: $OutputFile. See $DirectRunLog"
    }

    Write-Ma5TaskLog $LogDir $LogFile "Direct $ScriptName run completed and updated $OutputFile."
}

$logTailProcess = $null
$exitCode = 0

try {
    $startedAt = Get-Date
    Write-Ma5TaskLog $LogDir $LogFile "Starting direct watchcode task for intraday and premarket."
    if (-not (Test-Ma5TradingDayForTask $ProjectDir $LogDir $LogFile 1 "tomorrow watchcode generation")) {
        exit 0
    }

    $python = Resolve-Ma5Python $ProjectDir
    Write-Ma5TaskLog $LogDir $LogFile "Using python fallback: $($python.Label) at $($python.FilePath)"

    Start-Ma5PyCharm $ProjectDir $IntradayRunScript $LogDir $LogFile | Out-Null
    $logTailProcess = Start-Ma5LogTailWindow $DirectRunLog "MA5 watchcode logs" $LogDir $LogFile

    Invoke-Ma5WatchcodeScript $python $IntradayRunScript "watchcode_ma5.py" $WatchCodesFile "intraday watchcode" $startedAt
    Invoke-Ma5WatchcodeScript $python $PremarketRunScript "watchcode_premarket.py" $PremarketWatchCodesFile "premarket watchcode" $startedAt

    Write-Ma5TaskLog $LogDir $LogFile "Direct watchcode task completed for intraday and premarket."
} catch {
    Write-Ma5TaskLog $LogDir $LogFile "ERROR: $($_.Exception.Message)"
    $exitCode = 1
} finally {
    if ($null -ne $logTailProcess) {
        Start-Sleep -Seconds 2
        try {
            $logTailProcess.Refresh()
            if (-not $logTailProcess.HasExited) {
                Stop-Process -Id $logTailProcess.Id -Force
                $logTailProcess.WaitForExit(3000) | Out-Null
            }
            Write-Ma5TaskLog $LogDir $LogFile "Closed watchcode log tail window after task completion. PID: $($logTailProcess.Id)"
        } catch {
            Write-Ma5TaskLog $LogDir $LogFile "Could not close watchcode log tail window PID $($logTailProcess.Id): $($_.Exception.Message)"
        }
    }
}

exit $exitCode
