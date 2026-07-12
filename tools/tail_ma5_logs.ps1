param(
    [Parameter(Mandatory = $true)]
    [string[]]$Path,
    [string]$Title = "MA5 logs",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AdditionalPath
)

$ErrorActionPreference = "Stop"
if ($AdditionalPath) {
    $Path = @($Path) + $AdditionalPath
}

$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
try {
    chcp 65001 | Out-Null
} catch {
}

$Host.UI.RawUI.WindowTitle = $Title
Write-Host $Title
Write-Host ""
Write-Host "Log files:"
foreach ($item in $Path) {
    $parent = Split-Path -Parent $item
    if ($parent) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    if (-not (Test-Path $item)) {
        New-Item -ItemType File -Force -Path $item | Out-Null
    }
    Write-Host "  $item"
}
Write-Host ""
Write-Host "Press Ctrl+C to close this log window. It will not stop the background Python monitors."
Write-Host ""

Get-Content -LiteralPath $Path -Tail 80 -Wait -Encoding UTF8
