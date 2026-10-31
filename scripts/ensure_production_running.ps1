[CmdletBinding()]
param(
    [ValidateRange(1, 65535)][int]$Port = 8002,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$baseUrl = "http://127.0.0.1:$Port"
$dataDir = Join-Path $projectRoot 'data'
$logPath = Join-Path $dataDir "production-supervisor-$Port.log"
$retryPath = Join-Path $dataDir "production-supervisor-$Port-retries.json"
$runtimePath = Join-Path $dataDir "runtime-$Port.json"
$mutex = [Threading.Mutex]::new($false, "Local\EVProductionSupervisor$Port")
$locked = $false

function Write-SupervisorEvent([string]$Message) {
    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1048576) {
        @(Get-Content -LiteralPath $logPath -Tail 200) | Set-Content -LiteralPath $logPath -Encoding UTF8
    }
    Add-Content -LiteralPath $logPath -Value "$(Get-Date -Format o) $Message" -Encoding UTF8
}

function Read-Local([string]$Path) {
    try { return Invoke-RestMethod -Uri "$baseUrl$Path" -TimeoutSec 4 }
    catch { return $null }
}

function Port-Has-Listener {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::') } |
        Select-Object -First 1)
}

function Managed-Process-Alive {
    if (-not (Test-Path -LiteralPath $runtimePath)) { return $false }
    try {
        $runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$runtime.pid)"
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::') } | Select-Object -First 1
        $parent = if ($process) { Get-CimInstance Win32_Process -Filter "ProcessId=$($process.ParentProcessId)" }
        $expected = "-m evplatform --mode production --port $Port"
        $startedMatches = $true
        if ($process -and $runtime.started_at) {
            # The runtime receipt is millisecond precision; CIM retains
            # finer fractions. Compare the same process clock with a narrow
            # tolerance instead of requiring string equality.
            $reported = [datetimeoffset]$runtime.started_at
            $actual = [datetimeoffset]$process.CreationDate
            $startedMatches = [Math]::Abs(($actual - $reported).TotalSeconds) -lt 2
        }
        return [bool]($process -and $listener -and $listener.OwningProcess -eq $process.ProcessId -and
            $process.CommandLine -match [regex]::Escape($expected) -and
            $startedMatches -and
            ($process.ExecutablePath -eq (Join-Path $projectRoot '.venv\Scripts\python.exe') -or
             $parent.ExecutablePath -eq (Join-Path $projectRoot '.venv\Scripts\python.exe')))
    } catch { return $false }
}

function Invoke-OneShotGate {
    if ($Port -ne 8002) { return }
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    $recurring = Join-Path $dataDir 'native_recurring_authorization.json'
    $oneShot = Join-Path $dataDir 'native_one_shot_authorization.json'
    $oneShotStatus = Join-Path $dataDir 'native_one_shot_status.json'
    $gate = if (Test-Path -LiteralPath $oneShot) {
        Join-Path $PSScriptRoot 'watch_native_one_shot.py'
    } elseif (Test-Path -LiteralPath $recurring) {
        Join-Path $PSScriptRoot 'watch_native_recurring.py'
    } else {
        Join-Path $PSScriptRoot 'watch_native_one_shot.py'
    }
    if (-not (Test-Path -LiteralPath $python) -or -not (Test-Path -LiteralPath $gate)) {
        Write-SupervisorEvent 'One-shot gate unavailable; research scanner remains running.'
        return
    }
    # A one-minute scheduler tick can miss a brief qualified price. Sample the
    # existing local release gate for part of this tick; the scanner and OMS
    # still perform their independent per-order checks. No provider API is polled.
    $rapidPilot = Test-Path -LiteralPath $oneShot
    $started = Get-Date
    $sampleLimit = if ($rapidPilot) { 14 } else { 1 }
    for ($sample = 0; $sample -lt $sampleLimit; $sample++) {
        & $python $gate --url $baseUrl | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-SupervisorEvent "One-shot gate returned $LASTEXITCODE; inspect its status file."
            break
        }
        if (-not $rapidPilot) { break }
        try {
            $phase = (Get-Content -LiteralPath $oneShotStatus -Raw | ConvertFrom-Json).phase
        } catch {
            Write-SupervisorEvent 'One-shot status could not be read; rapid sampling stopped.'
            break
        }
        if ($phase -ne 'pending' -or ((Get-Date) - $started).TotalSeconds -ge 52) { break }
        Start-Sleep -Seconds 3
    }
}

try {
    try { $locked = $mutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { return }

    $health = Read-Local '/api/health'
    if ($health -and $health.mode -ne 'production') {
        throw "Port $Port is serving a different workspace; refusing to launch another scanner."
    }
    if ($CheckOnly) {
        [pscustomobject]@{
            port = $Port
            status = if ($health) { $health.status } else { 'unreachable' }
            critical_workers_stopped = @($health.critical_workers_stopped | Where-Object { $_ })
            plugin_workers_stopped = @($health.plugin_workers_stopped | Where-Object { $_ })
            listener = Port-Has-Listener
            managed_process_alive = Managed-Process-Alive
        }
        return
    }

    if ($health -and $health.status -eq 'running') {
        # Restart only workers that actually terminated, never an idle socket or
        # a feed whose own bounded reconnect is still running. Fifteen minutes
        # between attempts bounds paid snapshot and subscription usage.
        $stoppedPlugins = @($health.plugin_workers_stopped | Where-Object { $_ })
        if ($health.kill_switch -and $stoppedPlugins.Count -gt 0) {
            $retry = @{}
            if (Test-Path -LiteralPath $retryPath) {
                try {
                    $saved = Get-Content -LiteralPath $retryPath -Raw | ConvertFrom-Json
                    foreach ($entry in $saved.PSObject.Properties) { $retry[$entry.Name] = $entry.Value }
                } catch { Write-SupervisorEvent 'Invalid recovery cooldown file; no plugin restart attempted.'; return }
            }
            $allowed = @('parlay_ws', 'parlay_game_ws', 'parlay_rest', 'oddize_rest',
                'oddize_research_input', 'oddspapi_rest', 'kalshi', 'novig', 'prophetx', 'polymarket_us')
            foreach ($name in $stoppedPlugins) {
                if ($name -notin $allowed) { continue }
                $last = [datetimeoffset]::MinValue
                if ($retry.ContainsKey($name)) {
                    if (-not [datetimeoffset]::TryParse([string]$retry[$name], [ref]$last)) { continue }
                }
                $now = [datetimeoffset]::UtcNow
                if (($now - $last).TotalMinutes -lt 15) { continue }
                $retry[$name] = $now.ToString('o')
                $retry | ConvertTo-Json | Set-Content -LiteralPath $retryPath -Encoding UTF8
                try {
                    Invoke-RestMethod -Uri "$baseUrl/api/plugins/$name/toggle" -Method Post -TimeoutSec 30 `
                        -Headers @{ 'X-EV-Control' = 'local' } -ContentType 'application/json' `
                        -Body '{"enabled":true}' | Out-Null
                    Write-SupervisorEvent "Restarted stopped input worker: $name"
                } catch {
                    Write-SupervisorEvent "Input worker recovery failed: $name ($($_.Exception.GetType().Name))"
                }
            }
        }
        Invoke-OneShotGate
        return
    }

    if ($health -and $health.status -eq 'degraded') {
        $orders = Read-Local '/api/native/orders'
        $state = Read-Local '/api/state'
        $unresolved = @($orders.orders | Where-Object {
            $_.status -in @('intent', 'submitting', 'open', 'partial', 'cancel_pending', 'unknown')
        })
        if (-not $orders -or -not $state -or $unresolved.Count -gt 0 -or
            $state.auto_trade -ne $false -or $state.kill_switch -ne $true) {
            Write-SupervisorEvent 'Core worker stopped; recovery deferred until disarmed state and order inventory are known.'
            return
        }
        & (Join-Path $projectRoot 'stop_local.ps1') -Port $Port | Out-Null
        Write-SupervisorEvent ('Restarting after stopped core worker: ' +
            (@($health.critical_workers_stopped) -join ','))
        for ($i = 0; $i -lt 20 -and (Port-Has-Listener); $i++) { Start-Sleep -Seconds 1 }
    }

    if (Port-Has-Listener) {
        Write-SupervisorEvent 'Port occupied without a healthy production response; no duplicate instance launched.'
        return
    }
    if (Managed-Process-Alive) {
        # A new instance can be initializing its feeds and ledger before HTTP
        # is ready. Leave it alone; the next scheduled check can assess it.
        return
    }

    & (Join-Path $projectRoot 'start_native.ps1') -Environment production -Port $Port `
        -NativeOrderPath -NoBrowser -Background | Out-Null
    $state = Read-Local '/api/state'
    if (-not $state -or $state.mode -ne 'production' -or $state.sample_feed_enabled -ne $false) {
        throw 'The restarted scanner did not return the expected production state.'
    }
    if ($state.auto_trade -ne $false -or $state.kill_switch -ne $true) {
        Invoke-RestMethod -Uri "$baseUrl/api/control" -Method Post -TimeoutSec 10 `
            -Headers @{ 'X-EV-Control' = 'local' } -ContentType 'application/json' `
            -Body '{"auto_trade":false,"kill_switch":true}' | Out-Null
        throw 'Production restarted with unexpected execution controls; emergency stop was applied.'
    }
    Write-SupervisorEvent 'Started production scanner disarmed on port 8002.'
    Invoke-OneShotGate
} catch {
    Write-SupervisorEvent "Supervisor failed: $($_.Exception.GetType().Name): $($_.Exception.Message)"
    throw
} finally {
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
