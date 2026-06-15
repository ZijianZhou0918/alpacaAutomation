$ErrorActionPreference = "Stop"

$ProjectDir = "C:\Users\zzj\Desktop\alpaca_ma5_service"
$RunScript = Join-Path $ProjectDir "monitor_ma5_forever.py"
$PyCharmExe = "C:\Program Files\JetBrains\PyCharm 2026.1.1\bin\pycharm64.exe"
$LogDir = Join-Path $ProjectDir "outputs\logs"
$LogFile = Join-Path $LogDir ("pycharm_gui_task_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-TaskLog {
    param([string]$Message)
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Get-RunningMonitor {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object {
            $_.CommandLine -like "*alpaca_ma5_service*" -and
            $_.CommandLine -like "*monitor_ma5_forever.py*"
        }
}

function Get-ProjectPyCharmProcess {
    $processes = Get-Process -Name "pycharm64" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 } |
        Sort-Object StartTime -Descending

    $projectWindow = $processes |
        Where-Object { $_.MainWindowTitle -like "*alpaca_ma5_service*" } |
        Select-Object -First 1

    if ($projectWindow) {
        return $projectWindow
    }

    return $processes | Select-Object -First 1
}

try {
    Write-TaskLog "Starting PyCharm GUI monitor task."

    if (-not (Test-Path $PyCharmExe)) {
        throw "PyCharm executable not found: $PyCharmExe"
    }
    if (-not (Test-Path $RunScript)) {
        throw "Run script not found: $RunScript"
    }

    $existing = Get-RunningMonitor
    if ($existing) {
        $ids = ($existing | ForEach-Object { $_.ProcessId }) -join ", "
        Write-TaskLog "Monitor already running. Skip GUI start. PIDs: $ids"
        exit 0
    }

    Start-Process -FilePath $PyCharmExe -ArgumentList "`"$ProjectDir`"", "`"$RunScript`""

    $deadline = (Get-Date).AddSeconds(90)
    $pyCharm = $null
    do {
        Start-Sleep -Seconds 1
        $pyCharm = Get-ProjectPyCharmProcess
        if ($pyCharm -and $pyCharm.MainWindowTitle -like "*monitor_ma5_forever.py*") {
            break
        }
    } while ((Get-Date) -lt $deadline)

    if (-not $pyCharm) {
        throw "PyCharm project window was not found."
    }
    if ($pyCharm.MainWindowTitle -notlike "*monitor_ma5_forever.py*") {
        throw "PyCharm did not focus monitor_ma5_forever.py. Current title: $($pyCharm.MainWindowTitle)"
    }

    Add-Type -AssemblyName Microsoft.VisualBasic
    Add-Type -AssemblyName System.Windows.Forms

    $activated = $false
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if ([Microsoft.VisualBasic.Interaction]::AppActivate($pyCharm.Id)) {
            $activated = $true
            break
        }
        Start-Sleep -Milliseconds 800
    }
    if (-not $activated) {
        throw "Could not activate PyCharm window PID $($pyCharm.Id)."
    }

    Start-Sleep -Seconds 1
    [System.Windows.Forms.SendKeys]::SendWait("^+{F10}")
    Write-TaskLog "Sent Ctrl+Shift+F10 to PyCharm."

    Start-Sleep -Seconds 8
    $started = Get-RunningMonitor
    if ($started) {
        $ids = ($started | ForEach-Object { $_.ProcessId }) -join ", "
        Write-TaskLog "Monitor started through PyCharm GUI. PIDs: $ids"
        exit 0
    }

    throw "No monitor_ma5_forever.py process detected after GUI start."
} catch {
    Write-TaskLog "ERROR: $($_.Exception.Message)"
    exit 1
}
