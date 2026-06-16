$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$RunScript = Join-Path $ProjectDir "watchcode_ma5.py"
$WatchCodesFile = Join-Path $ProjectDir "watch_codes.txt"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("pycharm_watchcode_task_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$DirectRunLog = Join-Path $LogDir ("watchcode_direct_run_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$CommonScript = Join-Path $ProjectDir "tools\ma5_task_common.ps1"

if (-not (Test-Path $CommonScript)) {
    throw "Common task script not found: $CommonScript"
}
. $CommonScript

try {
    $startedAt = Get-Date
    Write-Ma5TaskLog $LogDir $LogFile "Starting direct watchcode task."

    if (-not (Test-Path $RunScript)) {
        throw "Run script not found: $RunScript"
    }
    $python = Resolve-Ma5Python $ProjectDir
    Write-Ma5TaskLog $LogDir $LogFile "Using python fallback: $($python.Label) at $($python.FilePath)"

    $existing = Get-Ma5PythonProcess $ProjectDir "watchcode_ma5.py"
    if ($existing) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "watchcode_ma5.py is already running. Skip direct start. PIDs: $ids"
        exit 0
    }

    Start-Ma5PyCharm $ProjectDir $RunScript $LogDir $LogFile | Out-Null

    Write-Ma5TaskLog $LogDir $LogFile "Running watchcode_ma5.py through direct python."
    Push-Location $ProjectDir
    try {
        $pythonExe = $python.FilePath
        $pythonArgs = @($python.Args + @($RunScript))
        & $pythonExe @pythonArgs *>> $DirectRunLog
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    if ($exitCode -ne 0) {
        throw "Direct watchcode_ma5.py run failed with exit code $exitCode. See $DirectRunLog"
    }
    if (-not (Test-Path $WatchCodesFile)) {
        throw "watchcode_ma5.py finished, but watch_codes.txt was not found."
    }

    $watchCodes = Get-Item $WatchCodesFile
    if ($watchCodes.LastWriteTime -lt $startedAt.AddSeconds(-2)) {
        throw "watchcode_ma5.py finished, but watch_codes.txt was not updated. See $DirectRunLog"
    }

    Write-Ma5TaskLog $LogDir $LogFile "Direct watchcode_ma5.py run completed and updated watch_codes.txt."
    exit 0
} catch {
    Write-Ma5TaskLog $LogDir $LogFile "ERROR: $($_.Exception.Message)"
    exit 1
}
