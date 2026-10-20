[CmdletBinding()]
param([int]$Port = 8001)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$runtimePath = Join-Path $PSScriptRoot "data\runtime-$Port.json"
if (-not (Test-Path -LiteralPath $runtimePath)) { throw "No managed EV process recorded for port $Port." }
$runtime = Get-Content -LiteralPath $runtimePath -Raw | ConvertFrom-Json
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($runtime.pid)"
if (-not $processInfo) { Write-Host "Recorded process is already stopped."; return }
$expectedPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ($processInfo.ExecutablePath -ne $expectedPython -or $processInfo.CommandLine -notmatch "evplatform") {
    throw "Recorded PID belongs to a different process. Refusing to stop it."
}
# Disable automation/cancel remaining simulated orders before ending the local worker.
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/control" -Method Post `
    -ContentType "application/json" -Headers @{"X-EV-Control"="local"} `
    -Body '{"auto_trade":false}' | Out-Null
Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/shutdown" -Method Post `
    -Headers @{"X-EV-Control"="local"} | Out-Null
Wait-Process -Id $runtime.pid -Timeout 20
Write-Host "Stopped EV Platform on port $Port. Durable state will reconcile on restart."

