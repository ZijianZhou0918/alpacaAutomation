$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$RunScript = Join-Path $ProjectDir "monitor_ma5_forever.py"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("pycharm_gui_task_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$CommonScript = Join-Path $ProjectDir "tools\ma5_task_common.ps1"

if (-not (Test-Path $CommonScript)) {
    throw "Common task script not found: $CommonScript"
}
. $CommonScript

try {
    Write-Ma5TaskLog $LogDir $LogFile "Starting direct monitor task."
    if (-not (Test-Ma5TradingDayForTask $ProjectDir $LogDir $LogFile 1 "tomorrow monitor startup")) {
        exit 0
    }

    if (-not (Test-Path $RunScript)) {
        throw "Run script not found: $RunScript"
    }
    $python = Resolve-Ma5Python $ProjectDir
    Write-Ma5TaskLog $LogDir $LogFile "Using python fallback: $($python.Label) at $($python.FilePath)"

    $existing = Get-Ma5PythonProcess $ProjectDir "monitor_ma5_forever.py"
    if ($existing) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "Monitor already running. Skip direct start. PIDs: $ids"
        exit 0
    }

    Start-Ma5PyCharm $ProjectDir $RunScript $LogDir $LogFile | Out-Null

    $monitorOut = Join-Path $LogDir ("monitor_ma5_forever_{0}.out.log" -f (Get-Date -Format "yyyyMMdd"))
    $monitorErr = Join-Path $LogDir ("monitor_ma5_forever_{0}.err.log" -f (Get-Date -Format "yyyyMMdd"))
    $pythonArgs = ConvertTo-Ma5ArgumentList @($python.Args + @("-u", $RunScript))
    $env:PYTHONIOENCODING = "utf-8"
    $process = Start-Process -FilePath $python.FilePath -ArgumentList $pythonArgs -WorkingDirectory $ProjectDir -WindowStyle Minimized -RedirectStandardOutput $monitorOut -RedirectStandardError $monitorErr -PassThru
    Write-Ma5TaskLog $LogDir $LogFile "Started monitor_ma5_forever.py through direct python. Starter PID: $($process.Id)"
    Write-Ma5TaskLog $LogDir $LogFile "Monitor stdout: $monitorOut"
    Write-Ma5TaskLog $LogDir $LogFile "Monitor stderr: $monitorErr"

    $started = Wait-Ma5PythonProcess $ProjectDir "monitor_ma5_forever.py" 30
    if ($started) {
        $ids = ($started | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "Monitor running. PIDs: $ids"
        exit 0
    }

    $process.Refresh()
    if (-not $process.HasExited) {
        Write-Ma5TaskLog $LogDir $LogFile "Monitor starter process is still alive. PID: $($process.Id)"
        exit 0
    }

    throw "No monitor_ma5_forever.py process detected after direct start."
} catch {
    Write-Ma5TaskLog $LogDir $LogFile "ERROR: $($_.Exception.Message)"
    exit 1
}
