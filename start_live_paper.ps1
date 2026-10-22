[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8001,
    [string]$DatabasePath = 'data\live-paper.db',
    [switch]$Paused,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$baseUrl = "http://127.0.0.1:$Port"
$controlHeaders = @{ 'X-EV-Control' = 'local' }
$database = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot $DatabasePath))
$dataRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'data'))
if (-not $database.StartsWith($dataRoot + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase) -or -not $database.EndsWith('.db', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The paper database must be a .db file under this workspace data folder.'
}
if ($database.Equals([IO.Path]::GetFullPath((Join-Path $dataRoot 'evplatform.db')),
        [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Use a separate paper database; data\evplatform.db contains the existing demo workspace.'
}

$previous = $null
try { $previous = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 5 } catch { }
if ($previous) {
    $native = Invoke-RestMethod -Uri "$baseUrl/api/native/orders" -TimeoutSec 5
    $runtimePath = Join-Path $PSScriptRoot "data\runtime-$Port.json"
    $runningDatabase = $null
    if (Test-Path -LiteralPath $runtimePath) {
        try {
            $runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
            if ($runtime.port -eq $Port -and $runtime.mode -eq 'paper' -and
                $runtime.database_path -is [string]) {
                $runningDatabase = [IO.Path]::GetFullPath($runtime.database_path)
            }
        } catch { }
    }
    $correctDatabase = $runningDatabase -and $runningDatabase.Equals($database,
        [StringComparison]::OrdinalIgnoreCase)
    if ($previous.mode -eq 'paper' -and $previous.sample_feed_enabled -eq $false -and
        $native.native_execution_enabled -eq $false -and @($native.orders).Count -eq 0 -and
        $correctDatabase) {
        & "$PSScriptRoot\setup_live_inputs.ps1" -Port $Port -EnablePaperAutomation:(-not $Paused)
        $previous = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
        Write-Host "Live-input paper workspace is already running at $baseUrl. Paper automation: $($previous.auto_trade)."
        if (-not $NoBrowser) { Start-Process -FilePath "$baseUrl/?page=providers" }
        return
    }
    if ($previous.mode -notin @('demo', 'paper') -or
        $native.native_execution_enabled -ne $false -or @($native.orders).Count -ne 0 -or
        @($previous.orders | Where-Object { $_.status -in @('open', 'partial', 'cancel_pending', 'unknown') }).Count -ne 0) {
        throw 'Stop active or native execution before changing the paper workspace; no account or order action was taken.'
    }
}

# Only carry enabled source and monitor preferences. Strategy, risk, order and
# execution settings stay in the separate paper database.
$monitorPreferences = @()
if ($previous) {
    $monitorPreferences = @($previous.plugins | Where-Object {
        if ($_.enabled -ne $true -or -not ($_.config -is [pscustomobject])) { return $false }
        $kind = $_.manifest.kind
        $capabilities = $_.manifest.capabilities
        return (($kind -eq 'input' -and $_.manifest.name -ne 'synthetic_schedule') -or
            ($kind -eq 'execution' -and $capabilities.orderbook -eq $true) -or
            ($kind -eq 'observer' -and $capabilities.monitor_only -eq $true))
    } | ForEach-Object { [pscustomobject]@{ name = $_.manifest.name; config = $_.config } })
    & "$PSScriptRoot\stop_local.ps1" -Port $Port
}

$priorDatabase = $env:EV_DATABASE_PATH
$priorSample = $env:EV_SAMPLE_FEED_ENABLED
$priorLive = $env:EV_LIVE_ENABLED
$priorAuto = $env:EV_AUTO_TRADE
try {
    $env:EV_DATABASE_PATH = $database
    $env:EV_SAMPLE_FEED_ENABLED = 'false'
    $env:EV_LIVE_ENABLED = 'false'
    $env:EV_AUTO_TRADE = 'false'
    & "$PSScriptRoot\start_paper.ps1" -Port $Port -NoBrowser -Background
} finally {
    $env:EV_DATABASE_PATH = $priorDatabase
    $env:EV_SAMPLE_FEED_ENABLED = $priorSample
    $env:EV_LIVE_ENABLED = $priorLive
    $env:EV_AUTO_TRADE = $priorAuto
}

$failed = @()
foreach ($monitor in $monitorPreferences) {
    $name = $monitor.name
    if ($name -notmatch '^[A-Za-z0-9_.-]+$') { $failed += $name; continue }
    try {
        $newState = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 5
        $current = @($newState.plugins | Where-Object { $_.manifest.name -eq $name })
        if ($current.Count -ne 1) { throw 'Provider is unavailable in this version' }
        $configJson = $monitor.config | ConvertTo-Json -Compress -Depth 30
        $currentJson = $current[0].config | ConvertTo-Json -Compress -Depth 30
        if ($configJson -ne $currentJson) {
            $body = @{ config = $monitor.config } | ConvertTo-Json -Compress -Depth 30
            Invoke-RestMethod -Uri "$baseUrl/api/plugins/$name/config" -Method Post -ContentType 'application/json' -Headers $controlHeaders -Body $body -TimeoutSec 20 | Out-Null
        }
        if ($current[0].enabled -ne $true) {
            Invoke-RestMethod -Uri "$baseUrl/api/plugins/$name/toggle" -Method Post -ContentType 'application/json' -Headers $controlHeaders -Body '{"enabled":true}' -TimeoutSec 30 | Out-Null
        }
    } catch {
        # One feed failure must not disable another. Provider Center shows the
        # current health without printing credential or server bodies.
        $failed += $name
    }
}

$state = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
$native = Invoke-RestMethod -Uri "$baseUrl/api/native/orders" -TimeoutSec 10
if ($state.mode -ne 'paper' -or $state.sample_feed_enabled -ne $false -or
    $state.auto_trade -ne $false -or $native.native_execution_enabled -ne $false -or
    @($native.orders).Count -ne 0) {
    throw 'The paper workspace did not enter the expected paused, sample-free state.'
}
& "$PSScriptRoot\setup_live_inputs.ps1" -Port $Port -EnablePaperAutomation:(-not $Paused)
$state = Invoke-RestMethod -Uri "$baseUrl/api/state" -TimeoutSec 10
Write-Host "Live-feed paper workspace running at $baseUrl." -ForegroundColor Green
Write-Host "Sample odds OFF. Paper automation: $($state.auto_trade). Native execution OFF."
Write-Host "Enabled source/monitor count: $(@($state.plugins | Where-Object { $_.enabled -and $_.health.synthetic -eq $false }).Count)."
if ($failed.Count) { Write-Warning "Could not restore these monitors: $($failed -join ', '). Review Provider Center." }
if ($state.kill_switch -and -not $Paused) { Write-Warning 'Emergency stop is active; paper automation remains off.' }
Write-Host 'Shared sportsbook profiles drive both live-input paper comparisons and native pricing.'
if (-not $NoBrowser) { Start-Process -FilePath "$baseUrl/?page=providers" }
