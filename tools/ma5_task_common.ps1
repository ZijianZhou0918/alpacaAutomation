$KnownPyCharmPaths = @(
    "C:\Program Files\JetBrains\PyCharm 2026.1.1\bin\pycharm64.exe",
    "C:\Program Files\JetBrains\PyCharm 2026.1\bin\pycharm64.exe",
    "C:\Program Files\JetBrains\PyCharm 2025.3\bin\pycharm64.exe"
)

function Write-Ma5TaskLog {
    param(
        [string]$LogDir,
        [string]$LogFile,
        [string]$Message
    )

    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

function Get-Ma5PythonProcess {
    param(
        [string]$ProjectDir,
        [string]$ScriptName
    )

    Get-CimInstance Win32_Process |
        Where-Object {
            $commandLine = [string]$_.CommandLine
            $_.Name -in @("python.exe", "pythonw.exe", "py.exe") -and
            $commandLine -like "*$ProjectDir*" -and
            $commandLine -like "*$ScriptName*"
        }
}

function Resolve-Ma5Python {
    param([string]$ProjectDir)

    $venvPython = Join-Path $ProjectDir ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return [pscustomobject]@{
            FilePath = $venvPython
            Args = @()
            Label = ".venv python"
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($python) {
        return [pscustomobject]@{
            FilePath = $python.Source
            Args = @()
            Label = "PATH python.exe"
        }
    }

    $py = Get-Command "py.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        return [pscustomobject]@{
            FilePath = $py.Source
            Args = @("-3")
            Label = "py -3"
        }
    }

    throw "Python executable not found. Tried .venv, python.exe, and py.exe."
}

function Resolve-PyCharmExe {
    foreach ($path in $KnownPyCharmPaths) {
        if (Test-Path $path) {
            return $path
        }
    }

    foreach ($commandName in @("pycharm64.exe", "pycharm.exe", "pycharm.cmd")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($command) {
            return $command.Source
        }
    }

    if ($env:LOCALAPPDATA) {
        $toolboxScripts = @(
            (Join-Path $env:LOCALAPPDATA "JetBrains\Toolbox\scripts\pycharm.cmd"),
            (Join-Path $env:LOCALAPPDATA "JetBrains\Toolbox\scripts\pycharm64.exe")
        )
        foreach ($path in $toolboxScripts) {
            if (Test-Path $path) {
                return $path
            }
        }
    }

    $roots = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($root in $roots) {
        $jetBrainsDir = Join-Path $root "JetBrains"
        if (-not (Test-Path $jetBrainsDir)) {
            continue
        }

        $matches = Get-ChildItem -Path $jetBrainsDir -Directory -Filter "PyCharm*" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending
        foreach ($dir in $matches) {
            $exe = Join-Path $dir.FullName "bin\pycharm64.exe"
            if (Test-Path $exe) {
                return $exe
            }
        }
    }

    return $null
}

function Get-RunningPyCharm {
    Get-Process -Name "pycharm64", "pycharm" -ErrorAction SilentlyContinue
}

function Quote-Ma5Argument {
    param([string]$Value)

    if ($Value -match '[\s"]') {
        return '"' + ($Value -replace '"', '\"') + '"'
    }
    return $Value
}

function ConvertTo-Ma5ArgumentList {
    param([string[]]$Arguments)

    ($Arguments | ForEach-Object { Quote-Ma5Argument $_ }) -join " "
}

function Start-Ma5PyCharm {
    param(
        [string]$ProjectDir,
        [string]$RunScript,
        [string]$LogDir,
        [string]$LogFile
    )

    $pyCharmExe = Resolve-PyCharmExe
    if (-not $pyCharmExe) {
        Write-Ma5TaskLog $LogDir $LogFile "PyCharm executable not found; direct python fallback will still run."
        return $false
    }

    $running = Get-RunningPyCharm
    if ($running) {
        $ids = ($running | ForEach-Object { $_.Id }) -join ", "
        Write-Ma5TaskLog $LogDir $LogFile "PyCharm already running. PIDs: $ids"
    } else {
        Write-Ma5TaskLog $LogDir $LogFile "PyCharm is not running; launching it now."
    }

    $pyCharmArgs = ConvertTo-Ma5ArgumentList @($ProjectDir, $RunScript)
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        try {
            Start-Process -FilePath $pyCharmExe -ArgumentList $pyCharmArgs
        } catch {
            Write-Ma5TaskLog $LogDir $LogFile "PyCharm launch attempt $attempt failed: $($_.Exception.Message)"
        }

        $deadline = (Get-Date).AddSeconds(25)
        while ((Get-Date) -lt $deadline) {
            $running = Get-RunningPyCharm
            if ($running) {
                $ids = ($running | ForEach-Object { $_.Id }) -join ", "
                Write-Ma5TaskLog $LogDir $LogFile "PyCharm is running. PIDs: $ids"
                return $true
            }
            Start-Sleep -Seconds 2
        }

        Write-Ma5TaskLog $LogDir $LogFile "PyCharm not detected after attempt $attempt."
    }

    Write-Ma5TaskLog $LogDir $LogFile "PyCharm could not be verified; direct python fallback will still run."
    return $false
}

function Wait-Ma5PythonProcess {
    param(
        [string]$ProjectDir,
        [string]$ScriptName,
        [int]$TimeoutSeconds
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $processes = Get-Ma5PythonProcess $ProjectDir $ScriptName
        if ($processes) {
            return $processes
        }
        Start-Sleep -Seconds 2
    }

    return $null
}

function Test-Ma5TradingDayForTask {
    param(
        [string]$ProjectDir,
        [string]$LogDir,
        [string]$LogFile,
        [int]$DateOffsetDays,
        [string]$Purpose
    )

    $checker = Join-Path $ProjectDir "tools\check_ma5_trading_day.py"
    if (-not (Test-Path $checker)) {
        throw "Trading day checker not found: $checker"
    }

    $targetDate = (Get-Date).Date.AddDays($DateOffsetDays).ToString("yyyy-MM-dd")
    $python = Resolve-Ma5Python $ProjectDir
    $pythonArgs = @($python.Args + @($checker, $targetDate))

    Write-Ma5TaskLog $LogDir $LogFile "Checking trading calendar for $Purpose target_date=$targetDate."
    $output = & $python.FilePath @pythonArgs 2>&1
    $exitCode = $LASTEXITCODE
    foreach ($line in $output) {
        Write-Ma5TaskLog $LogDir $LogFile "Trading calendar: $line"
    }

    if ($exitCode -eq 0) {
        Write-Ma5TaskLog $LogDir $LogFile "Trading calendar allows this task."
        return $true
    }
    if ($exitCode -eq 2) {
        Write-Ma5TaskLog $LogDir $LogFile "Trading calendar says non-trading day; skip this task."
        return $false
    }

    throw "Trading calendar check failed with exit code $exitCode."
}
