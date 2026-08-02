[CmdletBinding()]
param(
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$CommonScript = Join-Path $PSScriptRoot "ma5_task_common.ps1"
if (-not (Test-Path -LiteralPath $CommonScript)) {
    throw "Task helper not found: $CommonScript"
}
. $CommonScript

$Python = Resolve-Ma5Python $ProjectDir
$Entries = @(
    [pscustomobject]@{
        Label = "intraday"
        Path = Join-Path $ProjectDir "watchcode_ma5.py"
    }
)

foreach ($Entry in $Entries) {
    if (-not (Test-Path -LiteralPath $Entry.Path)) {
        throw "WatchCode entry not found: $($Entry.Path)"
    }
}

if ($ValidateOnly) {
    Write-Output "ProjectDir=$ProjectDir"
    Write-Output "Python=$($Python.Label)"
    Write-Output "Order=intraday"
    Write-Output "ValidationOnly=True"
    exit 0
}

$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("watchcode_daily_{0}.log" -f (Get-Date -Format "yyyyMMdd"))
$Mutex = New-Object System.Threading.Mutex($false, "Local\AlpacaMA5DailyWatchcodes")
$HasMutex = $false
$Failures = @()

try {
    $HasMutex = $Mutex.WaitOne(0)
    if (-not $HasMutex) {
        Write-Ma5TaskLog $LogDir $LogFile "Another daily WatchCode generation is already running; skip duplicate task."
        exit 0
    }

    Write-Ma5TaskLog $LogDir $LogFile "Daily WatchCode generation started. Order=intraday. Premarket screening is disabled."
    foreach ($Entry in $Entries) {
        Write-Ma5TaskLog $LogDir $LogFile "Starting $($Entry.Label) WatchCode: $($Entry.Path)"
        $PythonArgs = @($Python.Args + @("-u", $Entry.Path))
        $PreviousErrorActionPreference = $ErrorActionPreference
        try {
            # Windows PowerShell 5.1 wraps native stderr as NativeCommandError.
            # Keep collecting the complete Python traceback and decide success
            # exclusively from the native process exit code.
            $ErrorActionPreference = "Continue"
            & $Python.FilePath @PythonArgs 2>&1 | ForEach-Object {
                $Line = [string]$_
                Write-Output $Line
                Write-Ma5TaskLog $LogDir $LogFile "$($Entry.Label): $Line"
            }
            $ExitCode = $LASTEXITCODE
        } finally {
            $ErrorActionPreference = $PreviousErrorActionPreference
        }
        if ($ExitCode -ne 0) {
            $Failures += "$($Entry.Label) exit=$ExitCode"
            Write-Ma5TaskLog $LogDir $LogFile "$($Entry.Label) WatchCode failed with exit code $ExitCode; continuing to the next generator."
            continue
        }
        Write-Ma5TaskLog $LogDir $LogFile "$($Entry.Label) WatchCode completed."
    }

    if ($Failures.Count -gt 0) {
        Write-Ma5TaskLog $LogDir $LogFile ("Daily WatchCode generation finished with failures: " + ($Failures -join "; "))
        exit 1
    }
    Write-Ma5TaskLog $LogDir $LogFile "Daily WatchCode generation completed successfully."
    exit 0
} finally {
    if ($HasMutex) {
        $Mutex.ReleaseMutex()
    }
    $Mutex.Dispose()
}
