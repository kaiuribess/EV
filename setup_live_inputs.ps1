[CmdletBinding()]
param([ValidateRange(1, 65535)][int]$Port = 8001)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$baseUrl = "http://127.0.0.1:$Port"
$headers = @{ 'X-EV-Control' = 'local' }
$sports = @('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl', 'baseball_mlb')
$books = @('pinnacle', 'fanduel', 'draftkings', 'caesars', 'betonline', 'bet365')

$state = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
$native = Invoke-RestMethod -Uri "$baseUrl/api/native/orders" -TimeoutSec 10
if ($state.mode -ne 'paper' -or $state.sample_feed_enabled -ne $false -or
    $native.native_execution_enabled -ne $false -or @($native.orders).Count -ne 0) {
    throw 'Start the separate sample-free paper workspace before configuring live inputs. No account request was made.'
}
if ($state.auto_trade) {
    Invoke-RestMethod -Uri "$baseUrl/api/control" -Method Post -Headers $headers `
        -ContentType 'application/json' -Body '{"auto_trade":false}' -TimeoutSec 10 | Out-Null
}

function Configure-Input {
    param([string]$Name, [hashtable]$Changes)
    $current = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
    $plugins = @($current.plugins | Where-Object { $_.manifest.name -eq $Name })
    if ($plugins.Count -ne 1) { throw "Input plugin $Name is unavailable; restart this workspace with the current build." }
    $config = $plugins[0].config
    $before = $config | ConvertTo-Json -Compress -Depth 30
    foreach ($key in $Changes.Keys) {
        $existing = $config.$key
        if ($existing -is [System.ValueType] -and $existing -eq $Changes[$key]) { continue }
        $config.$key = $Changes[$key]
    }
    $after = $config | ConvertTo-Json -Compress -Depth 30
    if ($after -ne $before) {
        Write-Host "Updating $Name feed settings."
        $body = @{ config = $config } | ConvertTo-Json -Compress -Depth 30
        Invoke-RestMethod -Uri "$baseUrl/api/plugins/$Name/config" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body $body -TimeoutSec 30 | Out-Null
    } else {
        Write-Host "Keeping $Name feed settings."
    }
    if (-not $plugins[0].enabled) {
        Invoke-RestMethod -Uri "$baseUrl/api/plugins/$Name/toggle" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body '{"enabled":true}' -TimeoutSec 30 | Out-Null
    }
}

# Broad prop changes and a separately scoped, provider-confirmed game board use
# the same backend-only Parlay credential. A complete board does not establish
# quote-specific active status, settlement equivalence or EV.
Configure-Input 'parlay_ws' @{
    sports = $sports; bookmakers = $books; stream_kinds = @('prop'); markets = @();
    stream_limit = 1000; max_age_seconds = 30
}
Configure-Input 'parlay_game_ws' @{
    sports = $sports; bookmakers = $books; stream_kinds = @('game');
    markets = @('h2h', 'spreads', 'totals'); stream_limit = 1000; max_age_seconds = 30;
    include_props = $false
}
# REST is scheduled discovery. Four sports × (3 game-market credits + 3 prop
# credits) per 15-minute cycle is an estimated 24 Parlay credits/cycle;
# provider usage headers remain authoritative. Polling never marks rows active.
Configure-Input 'parlay_rest' @{
    sports = $sports; bookmakers = $books; markets = @('h2h', 'spreads', 'totals');
    include_props = $true; props_limit = 5000; max_age_seconds = 120;
    poll_interval_seconds = [double]900; session_request_budget = 80000
}

$current = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
if ($current.pricing.reference_profile_mode -ne 'strict') {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        throw 'Install the backend dependencies with setup_windows.ps1 before loading reference priors.'
    }
    & '.\.venv\Scripts\python.exe' 'scripts\prepare_reference_profiles.py' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The supplied reference-prior file failed validation.' }
    $pricing = Get-Content -LiteralPath 'data\reference_profiles.pricing.json' -Raw | ConvertFrom-Json
    $body = @{ config = $pricing } | ConvertTo-Json -Compress -Depth 50
    Invoke-RestMethod -Uri "$baseUrl/api/plugins/positive_ev/config" -Method Post -Headers $headers `
        -ContentType 'application/json' -Body $body -TimeoutSec 30 | Out-Null
}

$result = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
$native = Invoke-RestMethod -Uri "$baseUrl/api/native/orders" -TimeoutSec 10
if ($result.auto_trade -ne $false -or $native.native_execution_enabled -ne $false) {
    throw 'The paper/native execution switches changed during source setup. Stop the application and inspect its state.'
}
Write-Host 'Configured Parlay prop stream, complete game-board stream, and 15-minute REST discovery.' -ForegroundColor Green
Write-Host 'Research reference profiles are loaded if this workspace previously used legacy default weights.'
Write-Host "Sample odds OFF. Paper automation OFF. Native execution OFF. Dashboard: $baseUrl"
Write-Host 'Source feeds may need several seconds to authenticate and publish a snapshot. Check Provider Center.'
Write-Host 'No mapping or current quote status was approved by this setup. Real EV signals remain blocked until the evidence gates pass.'
