[CmdletBinding()]
param(
    [ValidateSet("sandbox", "production")][string]$Environment = "sandbox",
    [int]$Port = 8002,
    [switch]$NoBrowser,
    [switch]$Background
)
# One feed/profile setup serves paper and native pricing. Native orders remain
# paused and still require their independent launch evidence and owner action.
$ErrorActionPreference = 'Stop'
$priorSample = $env:EV_SAMPLE_FEED_ENABLED
$priorLive = $env:EV_LIVE_ENABLED
$priorAuto = $env:EV_AUTO_TRADE
try {
    $env:EV_SAMPLE_FEED_ENABLED = 'false'
    $env:EV_LIVE_ENABLED = 'false'
    $env:EV_AUTO_TRADE = 'false'
    & "$PSScriptRoot\scripts\start_local.ps1" -Mode $Environment -Port $Port -NoBrowser -Background
} finally {
    $env:EV_SAMPLE_FEED_ENABLED = $priorSample
    $env:EV_LIVE_ENABLED = $priorLive
    $env:EV_AUTO_TRADE = $priorAuto
}
& "$PSScriptRoot\setup_live_inputs.ps1" -Port $Port
if (-not $NoBrowser) { Start-Process -FilePath "http://127.0.0.1:$Port/?page=providers" }
if (-not $Background) {
    Write-Host "Native workspace remains paused at http://127.0.0.1:$Port. Stop with .\stop_local.ps1 -Port $Port."
    $runtime = Get-Content -LiteralPath (Join-Path $PSScriptRoot "data\runtime-$Port.json") -Raw | ConvertFrom-Json
    Wait-Process -Id $runtime.pid
}
