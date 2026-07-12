$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$RunScript = Join-Path $ProjectDir "monitor_auto.py"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("pycharm_gui_task_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$CommonScript = Join-Path $ProjectDir "tools\ma5_task_common.ps1"

if (-not (Test-Path $CommonScript)) {
    throw "Common task script not found: $CommonScript"
}
. $CommonScript

try {
    Write-Ma5TaskLog $LogDir $LogFile "Starting single auto monitor task."
    if (-not (Test-Ma5TradingDayForTask $ProjectDir $LogDir $LogFile 0 "today auto monitor startup")) {
        exit 0
    }

    if (-not (Test-Path $RunScript)) {
        throw "Run script not found: $RunScript"
    }
    $python = Resolve-Ma5Python $ProjectDir
    Write-Ma5TaskLog $LogDir $LogFile "Using python fallback: $($python.Label) at $($python.FilePath)"

    $logDate = Get-Date -Format "yyyyMMdd"
    $monitorOut = Join-Path $LogDir ("monitor_auto_{0}.out.log" -f $logDate)
    $monitorErr = Join-Path $LogDir ("monitor_auto_{0}.err.log" -f $logDate)

    $existingAuto = Get-Ma5PythonProcess $ProjectDir "monitor_auto.py"
    if ($existingAuto) {
        $ids = ($existingAuto | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "monitor_auto.py already running. Skip direct start. PIDs: $ids"
        exit 0
    }

    $pythonArgs = ConvertTo-Ma5ArgumentList @($python.Args + @("-u", $RunScript))
    $env:PYTHONIOENCODING = "utf-8"
    $process = Start-Process -FilePath $python.FilePath -ArgumentList $pythonArgs -WorkingDirectory $ProjectDir -WindowStyle Normal -PassThru
    Write-Ma5TaskLog $LogDir $LogFile "Started monitor_auto.py through direct python. Starter PID: $($process.Id)"
    Write-Ma5TaskLog $LogDir $LogFile "Auto monitor visible Python window started; stdout is also tee'd to $monitorOut"
    Write-Ma5TaskLog $LogDir $LogFile "Auto monitor stderr is also tee'd to $monitorErr"

    $started = Wait-Ma5PythonProcess $ProjectDir "monitor_auto.py" 30
    if ($started) {
        $ids = ($started | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "Auto monitor running. PIDs: $ids"
        exit 0
    }

    $process.Refresh()
    if (-not $process.HasExited) {
        Write-Ma5TaskLog $LogDir $LogFile "Auto monitor starter process is still alive. PID: $($process.Id)"
        exit 0
    }

    throw "No monitor_auto.py process detected after direct start."
} catch {
    Write-Ma5TaskLog $LogDir $LogFile "ERROR: $($_.Exception.Message)"
    exit 1
}
