[CmdletBinding()]
param([int]$Port = 8001)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$runtimePath = Join-Path $PSScriptRoot "data\runtime-$Port.json"
if (-not (Test-Path -LiteralPath $runtimePath)) { throw "No managed EV process recorded for port $Port." }
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($runtime.pid)"
$expectedPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::') } | Select-Object -First 1
if (-not $listener) { Write-Host "Port $Port is already stopped."; return }
$bound = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
$parent = if ($bound) { Get-CimInstance Win32_Process -Filter "ProcessId = $($bound.ParentProcessId)" }
$expectedCommand = "-m evplatform --mode $($runtime.mode) --port $Port"
$startedMatches = $true
if ($bound -and $runtime.started_at) {
    $reported = [datetimeoffset]$runtime.started_at
    $actual = [datetimeoffset]$bound.CreationDate
    $startedMatches = [Math]::Abs(($actual - $reported).TotalSeconds) -lt 2
}
$sameRecordedProcess = ($processInfo -and $bound.ProcessId -eq $processInfo.ProcessId -and $startedMatches)
$verifiedVenvChild = ($parent -and $parent.ExecutablePath -eq $expectedPython -and
    $parent.CommandLine -match [regex]::Escape($expectedCommand))
if (-not $bound -or $bound.CommandLine -notmatch [regex]::Escape($expectedCommand) -or
    (-not $sameRecordedProcess -and -not $verifiedVenvChild)) {
    throw "Port $Port does not belong to the recorded EV workspace. Refusing to stop it."
}
# Disable automation/cancel remaining simulated orders before ending the local worker.
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/control" -Method Post `
    -ContentType "application/json" -Headers @{"X-EV-Control"="local"} `
    -Body '{"auto_trade":false}' | Out-Null
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/shutdown" -Method Post `
    -Headers @{"X-EV-Control"="local"} | Out-Null
Wait-Process -Id $bound.ProcessId -Timeout 20
Write-Host "Stopped EV Platform on port $Port. Durable state will reconcile on restart."

