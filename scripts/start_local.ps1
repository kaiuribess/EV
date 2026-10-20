[CmdletBinding()]
param(
    [ValidateSet("demo", "paper")][string]$Mode = "demo",
    [int]$Port = 8001,
    [switch]$AutoTrade,
    [switch]$NoBrowser,
    [switch]$Background
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe") -or -not (Test-Path -LiteralPath "frontend\dist\index.html")) {
    & ".\setup_windows.ps1"
}
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dashboardUrl = "http://127.0.0.1:$Port"
try {
    $existing = Invoke-RestMethod -Uri "$dashboardUrl/api/health" -TimeoutSec 2
    if ($existing.status -eq "running") {
        Write-Host "An EV Platform instance is already running at $dashboardUrl in $($existing.mode) mode."
        if (-not $NoBrowser) { Start-Process -FilePath $dashboardUrl }
        return
    }
} catch { }
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try { $probe.Start() } catch { throw "Port $Port is occupied. Run .\start_$Mode.ps1 -Port $($Port + 1)." }
finally { $probe.Stop() }
$oldAuto = $env:EV_AUTO_TRADE
$oldPort = $env:EV_PORT
try {
    $env:EV_AUTO_TRADE = if ($AutoTrade) { "true" } else { "false" }
    $env:EV_PORT = [string]$Port
    $launchArgs = @("-m", "evplatform", "--mode", $Mode, "--port", [string]$Port)
    if ($AutoTrade) { $launchArgs += "--auto-trade" }
    $stdoutPath = Join-Path $projectRoot "data\server-$Port.out.log"
    $stderrPath = Join-Path $projectRoot "data\server-$Port.err.log"
    $server = Start-Process -FilePath $pythonPath -ArgumentList $launchArgs -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
} finally {
    $env:EV_AUTO_TRADE = $oldAuto
    $env:EV_PORT = $oldPort
}
$ready = $false
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ($server.HasExited) { throw "Backend stopped. See data\server-$Port.err.log." }
    try {
        $health = Invoke-RestMethod -Uri "$dashboardUrl/api/health" -TimeoutSec 1
        if ($health.status -eq "running") { $ready = $true; break }
    } catch { }
    Start-Sleep -Milliseconds 500
}
if (-not $ready) { throw "Backend did not become ready. See data\server-$Port.err.log; process ID $($server.Id)." }
@{ pid = $server.Id; port = $Port; mode = $Mode; url = $dashboardUrl } |
    ConvertTo-Json | Set-Content -LiteralPath "data\runtime-$Port.json" -Encoding UTF8
Write-Host "EV Platform $Mode is running at $dashboardUrl (PID $($server.Id))." -ForegroundColor Green
Write-Host "Every demo price and paper fill is synthetic. Paper automation: $([bool]$AutoTrade)."
if (-not $NoBrowser) { Start-Process -FilePath $dashboardUrl }
if (-not $Background) {
    Write-Host "Press Ctrl+C to return to PowerShell; the hidden backend remains running."
    Write-Host "Stop safely with .\stop_local.ps1 -Port $Port."
    Wait-Process -Id $server.Id
}

