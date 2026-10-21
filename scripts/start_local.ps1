[CmdletBinding()]
param(
    [ValidateSet("demo", "paper", "sandbox", "production")][string]$Mode = "demo",
    [ValidateRange(1, 65535)][int]$Port = 8001,
    [switch]$AutoTrade,
    [switch]$NoBrowser,
    [switch]$Background
)
$ErrorActionPreference = "Stop"
if ($AutoTrade -and $Mode -notin @("demo", "paper")) {
    throw "Native execution must pass the running application's launch checks and be armed explicitly."
}
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe") -or -not (Test-Path -LiteralPath "frontend\dist\index.html")) {
    & ".\setup_windows.ps1"
}
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$dashboardUrl = "http://127.0.0.1:$Port"
$existing = $null
try {
    $existing = Invoke-RestMethod -Uri "$dashboardUrl/api/health" -TimeoutSec 2
} catch { }
if ($existing -and $existing.status -eq "running") {
    if ($existing.mode -ne $Mode) {
        throw "Port $Port already runs $($existing.mode), but $Mode was requested. Choose another port or stop that workspace explicitly."
    }
    Write-Host "An EV Platform instance is already running at $dashboardUrl in $($existing.mode) mode."
    if (-not $NoBrowser) { Start-Process -FilePath $dashboardUrl }
    return
}
$probe = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
try { $probe.Start() } catch {
    $nextPort = if ($Port -eq 65535) { 65534 } else { $Port + 1 }
    $retryCommand = if ($Mode -in @("sandbox", "production")) {
        ".\start_native.ps1 -Environment $Mode -Port $nextPort"
    } else { ".\start_$Mode.ps1 -Port $nextPort" }
    throw "Port $Port is occupied. Run $retryCommand."
}
finally { $probe.Stop() }
$oldAuto = $env:EV_AUTO_TRADE
$oldPort = $env:EV_PORT
$oldDatabase = $env:EV_DATABASE_PATH
try {
    $env:EV_AUTO_TRADE = if ($AutoTrade) { "true" } else { "false" }
    $env:EV_PORT = [string]$Port
    if ($Mode -in @("sandbox", "production")) {
        $env:EV_DATABASE_PATH = Join-Path $projectRoot "data\native-$Mode.db"
    }
    $launchArgs = @("-m", "evplatform", "--mode", $Mode, "--port", [string]$Port)
    if ($AutoTrade) { $launchArgs += "--auto-trade" }
    New-Item -ItemType Directory -Path (Join-Path $projectRoot "data") -Force | Out-Null
    $stdoutPath = Join-Path $projectRoot "data\server-$Port.out.log"
    $stderrPath = Join-Path $projectRoot "data\server-$Port.err.log"
    $server = Start-Process -FilePath $pythonPath -ArgumentList $launchArgs -WorkingDirectory $projectRoot `
        -WindowStyle Hidden -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath -PassThru
} finally {
    $env:EV_AUTO_TRADE = $oldAuto
    $env:EV_PORT = $oldPort
    $env:EV_DATABASE_PATH = $oldDatabase
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
if ($Mode -in @("demo", "paper")) {
    Write-Host "Every demo price and paper fill is synthetic. Paper automation: $([bool]$AutoTrade)."
} else {
    Write-Host "Native workspace started PAUSED. Review /api/readiness; credentials do not enable trading."
}
if (-not $NoBrowser) { Start-Process -FilePath $dashboardUrl }
if (-not $Background) {
    Write-Host "Press Ctrl+C to return to PowerShell; the hidden backend remains running."
    Write-Host "Stop safely with .\stop_local.ps1 -Port $Port."
    Wait-Process -Id $server.Id
}

