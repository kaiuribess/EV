[CmdletBinding()]
param([ValidateRange(1, 65535)][int]$Port = 8001, [switch]$EnablePaperAutomation)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$baseUrl = "http://127.0.0.1:$Port"
$headers = @{ 'X-EV-Control' = 'local' }
$sports = @('basketball_nba', 'americanfootball_nfl', 'icehockey_nhl', 'baseball_mlb')
$books = @('pinnacle', 'fanduel', 'draftkings', 'caesars', 'betonline', 'bet365')

$state = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
$native = Invoke-RestMethod -Uri "$baseUrl/api/native/orders" -TimeoutSec 10
$oddizePaperWasEnabled = @($state.plugins | Where-Object { $_.manifest.name -eq 'oddize_paper_input' -and $_.enabled }).Count -eq 1
$oddizeRestWasEnabled = @($state.plugins | Where-Object { $_.manifest.name -eq 'oddize_rest' -and $_.enabled }).Count -eq 1
$parlayPaperWasEnabled = @($state.plugins | Where-Object { $_.manifest.name -eq 'parlay_paper_input' -and $_.enabled }).Count -eq 1
$parlayGameWasEnabled = @($state.plugins | Where-Object { $_.manifest.name -eq 'parlay_game_ws' -and $_.enabled }).Count -eq 1
$isPaper = $state.mode -eq 'paper' -and $state.sample_feed_enabled -eq $false -and
    $native.native_execution_enabled -eq $false -and @($native.orders).Count -eq 0
$isNative = $state.mode -in @('sandbox', 'production') -and $state.sample_feed_enabled -eq $false -and
    $state.auto_trade -eq $false -and
    @($native.orders | Where-Object { $_.status -in @('intent', 'submitting', 'open', 'partial', 'cancel_pending', 'unknown') }).Count -eq 0
if (-not $isPaper -and -not $isNative) {
    throw 'Start a paused sample-free paper or native workspace before configuring shared live inputs. No account request was made.'
}
if ($EnablePaperAutomation -and -not $isPaper) {
    throw 'Paper automation is only available in the paper workspace. Native order authorization is never changed here.'
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
    stream_limit = 1000; max_age_seconds = 30; account_tier = 'unknown'
}
Configure-Input 'parlay_game_ws' @{
    sports = $sports; bookmakers = $books; stream_kinds = @('game');
    markets = @('h2h', 'spreads', 'totals'); stream_limit = 1000; max_age_seconds = 600;
    baseline_refresh_seconds = 300; include_props = $false; account_tier = 'unknown'
}
if ($isPaper) {
    Configure-Input 'parlay_paper_input' @{
        sports = $sports; bookmakers = $books; stream_kinds = @('game');
        markets = @('h2h', 'spreads', 'totals'); stream_limit = 1000;
        max_age_seconds = 600; max_poll_age_seconds = 180;
        include_props = $false; account_tier = 'unknown'
    }
    Configure-Input 'oddize_paper_input' @{
        sports = @('nba'); max_observation_age_seconds = 600;
        max_start_difference_minutes = 15
    }
    Configure-Input 'parlay_paper_ev' @{ max_quote_age_ms = 600000; min_references = 2 }
}
# Start the consumer before the REST source on a fresh paper workspace, so
# its first metered snapshot need not be fetched twice.
Configure-Input 'oddize_rest' @{
    sports = @('nba'); bookmakers = @('pin', 'circa', 'dk', 'fd', 'czr', 'b365', 'mgm'); markets = @('ml');
    poll_interval_seconds = [double]3600; session_request_budget = 1200;
    monthly_credit_budget = 1200; include_past = $false;
    use_delta_polling = $false; include_props = $false
}
# REST is scheduled discovery. Four sports × (3 game-market credits + 3 prop
# credits) per 15-minute cycle is an estimated 24 Parlay credits/cycle;
# provider usage headers remain authoritative. Polling never marks rows active.
# Do not filter REST props by last price-change age: unchanged prices remain
# visible for review, while strict quote-clock/status checks control eligibility.
Configure-Input 'parlay_rest' @{
    sports = $sports; bookmakers = $books; markets = @('h2h', 'spreads', 'totals');
    include_props = $true; props_limit = 5000;
    deduplicate_complete_game_stream = $true; startup_stream_wait_seconds = 12;
    poll_interval_seconds = [double]900; session_request_budget = 80000;
    focused_novig_nfl_props = ($isNative -and $state.mode -eq 'production');
    focused_props_interval_seconds = 120; focused_props_hourly_credit_budget = 36;
    focused_props_limit = 200;
    live_points_enabled = ($isNative -and $state.mode -eq 'production');
    live_points_hourly_budget = 12; live_points_interval_seconds = 90;
    account_tier = 'unknown'
}

# The free OddsPapi account is pregame-only and has a small shared monthly
# allowance. A native one-market pilot with explicitly selected fixture IDs
# spends its limited fixture requests on those IDs before broad discovery;
# otherwise collect at most two upcoming NBA fixtures per day. Stop while
# 150 provider requests remain. These quotes are research inputs until exact
# market/settlement mappings and source freshness are independently verified.
$oddspapiKeyConfigured = [bool]$env:ODDSPAPI_API_KEY -or
    ((Test-Path -LiteralPath '.env') -and
     (Select-String -LiteralPath '.env' -Pattern '^ODDSPAPI_API_KEY=\S+' -Quiet))
if ($oddspapiKeyConfigured) {
    $oddspapiPlugin = @($state.plugins | Where-Object { $_.manifest.name -eq 'oddspapi_rest' })
    $targetedFixtureCount = if ($isNative -and $oddspapiPlugin.Count -eq 1) {
        @($oddspapiPlugin[0].config.fixture_ids | Where-Object { $_ }).Count
    } else { 0 }
    $fixtureRequestCap = if ($targetedFixtureCount -gt 0) { $targetedFixtureCount } else { 2 }
    Configure-Input 'oddspapi_rest' @{
        sports = @('11'); bookmakers = @('pinnacle', 'fanduel', 'draftkings', 'circasports');
        tournament_id = 132;
        include_props = $true; include_live = $false;
        poll_interval_seconds = [double]86400;
        discovery_interval_seconds = [double]86400;
        discovery_window_hours = 24;
        max_fixture_requests_per_cycle = $fixtureRequestCap;
        provider_remaining_floor = 150;
        session_request_budget = 100;
        max_requests_per_minute = 10
    }
}

# Read-only native catalogs and books are useful before any execution review.
# Keep the bounded subscriptions separate from source/research and never add a
# reviewed mapping or authorization here. The same source setup runs on paper,
# so native monitors are changed only in the stopped production workspace.
if ($isNative -and $state.mode -eq 'production') {
    Configure-Input 'kalshi' @{
        discovery_series = @('KXNBAGAME', 'KXNFLGAME', 'KXMLBGAME', 'KXNHLGAME', 'KXMLBTOTAL');
        max_discovered_markets = 16
    }
    Configure-Input 'novig' @{
        discovery_leagues = @('NBA', 'NFL', 'MLB', 'NHL');
        discovery_market_types = @('MONEY', 'POINTS', 'REBOUNDS', 'ASSISTS',
            'THREE_POINTERS_MADE', 'POINTS_REBOUNDS_ASSISTS', 'PASSING_YARDS',
            'RUSHING_YARDS', 'RECEIVING_YARDS', 'RECEPTIONS', 'PITCHER_STRIKEOUTS',
            'HITS', 'TOTAL_BASES', 'RBIS', 'RUNS', 'SHOTS_ON_GOAL');
        discovery_page_size = 2000; max_discovered_markets = 16
    }
    # Keep the reviewed NBA tournament while ranking current tournaments
    # across supported two-way research sports. Discovery still selects at
    # most four actual tournament IDs and does not approve execution.
    Configure-Input 'prophetx' @{
        discovery_leagues = @('NFL', 'NBA', 'MLB', 'NHL', 'NCAAF', 'NCAABM',
            'UFC', 'ATP', 'WTA')
        discovery_lookahead_hours = 96
    }
}

$current = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
if ($current.pricing.reference_profile_mode -ne 'strict') {
    if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
        throw 'Install the backend dependencies with setup_windows.ps1 before loading reference priors.'
    }
    & '.\.venv\Scripts\python.exe' 'scripts\prepare_reference_profiles.py' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'The supplied reference-prior file failed validation.' }
    $pricing = Get-Content -LiteralPath 'data\reference_profiles.pricing.json' -Raw | ConvertFrom-Json
} else {
    $pricing = $current.pricing
}
$mainlineProfiles = @('nba_nfl_mainlines', 'mlb_mainlines', 'nhl_mainlines_experimental')
$enabled = @($pricing.enabled_reference_profiles)
foreach ($name in $mainlineProfiles) {
    if (-not ($pricing.reference_profiles.PSObject.Properties.Name -contains $name)) {
        throw "Shared reference profile $name is unavailable. No order authorization was changed."
    }
    if ($enabled -notcontains $name) { $enabled += $name }
}
if (@($pricing.enabled_reference_profiles).Count -ne $enabled.Count -or $current.pricing.reference_profile_mode -ne 'strict') {
    $pricing.enabled_reference_profiles = $enabled
    $body = @{ config = $pricing } | ConvertTo-Json -Compress -Depth 50
    Invoke-RestMethod -Uri "$baseUrl/api/plugins/positive_ev/config" -Method Post -Headers $headers `
        -ContentType 'application/json' -Body $body -TimeoutSec 30 | Out-Null
}

# The shared overlap and unit preferences apply to both workspaces. Paper has
# no authenticated account cash, so its simulated stake stays capped at $5.
# Native launch profiles own their reviewed sizing and cash policy. Feed setup
# must preserve that policy across a restart.
$riskChanges = @{ block_overlapping_events = $true; block_overlapping_players = $true;
    unit_size_usd = '5' }
$fixedNativeCash = $isNative -and $current.risk.cash_policy -eq 'prophetx_exchange_rejection'
if ($isPaper) {
    $riskChanges.max_order_stake = '5'
    $riskChanges.daily_loss_limit = $null
} elseif (-not $fixedNativeCash) {
    $riskChanges.available_cash_only = $true
    $riskChanges.stake_sizing = 'quarter_kelly'
    # This script configures shared input feeds. Preserve any stricter native
    # stake, exposure, rate and lifetime-attempt limits already saved by an
    # operator; a restart must not silently remove the pilot's risk cap.
}
$riskBody = $riskChanges | ConvertTo-Json -Compress
try {
    Invoke-RestMethod -Uri "$baseUrl/api/settings" -Method Post -Headers $headers `
        -ContentType 'application/json' -Body $riskBody -TimeoutSec 30 | Out-Null
} catch {
    # A busy live scanner can complete an idempotent settings write after the
    # HTTP client times out. Verify persisted state instead of blindly retrying.
    if (-not $isNative) { throw }
    $after = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 30
    $cashModeMatches = if ($fixedNativeCash) {
        $after.risk.cash_policy -eq 'prophetx_exchange_rejection' -and
        $after.risk.available_cash_only -eq $false -and
        $after.risk.stake_sizing -eq 'capped'
    } else {
        $after.risk.available_cash_only -eq $true -and
        $after.risk.stake_sizing -eq 'quarter_kelly'
    }
    if ($after.auto_trade -ne $false -or $after.kill_switch -ne $true -or
        $after.risk.block_overlapping_events -ne $true -or
        $after.risk.block_overlapping_players -ne $true -or
        -not $cashModeMatches -or
        [decimal]$after.risk.unit_size_usd -ne [decimal]5) { throw }
    Write-Host 'Verified persisted native risk settings after delayed control response.'
}

if ($isPaper) {
    # A newly started consumer may have missed the game's initial snapshot.
    # Repeating setup on an already synchronized workspace must not interrupt
    # the only live game board merely to refresh configuration.
    if (-not $parlayPaperWasEnabled -or -not $parlayGameWasEnabled) {
        Invoke-RestMethod -Uri "$baseUrl/api/plugins/parlay_game_ws/toggle" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body '{"enabled":false}' -TimeoutSec 30 | Out-Null
        Invoke-RestMethod -Uri "$baseUrl/api/plugins/parlay_game_ws/toggle" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body '{"enabled":true}' -TimeoutSec 30 | Out-Null
    }
    if ($oddizeRestWasEnabled -and -not $oddizePaperWasEnabled) {
        # The newly enabled consumer missed the older source snapshot. Fetch
        # one new metered snapshot; subsequent setup runs keep the poll timer.
        Invoke-RestMethod -Uri "$baseUrl/api/plugins/oddize_rest/toggle" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body '{"enabled":false}' -TimeoutSec 30 | Out-Null
        Invoke-RestMethod -Uri "$baseUrl/api/plugins/oddize_rest/toggle" -Method Post -Headers $headers `
            -ContentType 'application/json' -Body '{"enabled":true}' -TimeoutSec 30 | Out-Null
    }
}

$result = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
$native = Invoke-RestMethod -Uri "$baseUrl/api/native/orders" -TimeoutSec 10
if ($result.auto_trade -ne $false -or ($isPaper -and $native.native_execution_enabled -ne $false)) {
    throw 'An execution switch changed during source setup. Stop the application and inspect its state.'
}
if ($EnablePaperAutomation) {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $result = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
        $paper = @($result.plugins | Where-Object { $_.manifest.name -eq 'parlay_paper_input' })[0]
        if ($paper.enabled -and @($paper.health.complete_baseline_sports).Count -gt 0) { break }
        Start-Sleep -Seconds 1
    }
    if (-not $paper.enabled -or @($paper.health.complete_baseline_sports).Count -eq 0) {
        throw 'No complete Parlay paper baseline arrived. Paper automation remains paused; inspect Provider Center.'
    }
    $result = Invoke-RestMethod -Uri "$baseUrl/api/control" -Method Post -Headers $headers `
        -ContentType 'application/json' -Body '{"auto_trade":true}' -TimeoutSec 10
}
Write-Host 'Configured Parlay streaming/discovery and hourly Oddize NBA REST references.' -ForegroundColor Green
if ($oddspapiKeyConfigured) {
    Write-Host 'Configured quota-limited OddsPapi pregame NBA research snapshots; free-tier in-play access is not available.'
}
Write-Host 'The same research profiles and weights are configured for paper and native workspaces.'
Write-Host "Workspace: $($result.mode). Automatic orders: $($result.auto_trade). Dashboard: $baseUrl"
Write-Host 'Source feeds may need several seconds to authenticate and publish a snapshot. Check Provider Center.'
if ($isPaper) {
    Write-Host 'Paper comparison: Parlay targets plus matched Oddize NBA references; hypothetical offers, capacity and fills; $5 simulated stake cap.'
} else {
    if ($fixedNativeCash) {
        Write-Host 'Native risk: reviewed ProphetX fixed-stake exchange-rejection cash policy preserved; $5 reporting unit.'
    } else {
        Write-Host 'Native risk: quarter-Kelly, verified available cash, $5 reporting unit.'
    }
}
Write-Host "Automatic native orders: $($result.auto_trade). Current source, market and launch checks remain mandatory."
