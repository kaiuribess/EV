[CmdletBinding()]
param(
    [ValidateSet('status', 'prepare', 'open', 'reconcile', 'fund', 'recover-funding', 'export')]
    [string]$Action = 'status',
    [string]$Journal,
    [string]$Amount,
    [string]$Transfer,
    [string]$Label = 'EV Platform'
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run .\setup_windows.ps1 first to install the local Python environment.'
}
$arguments = @('scripts/setup_novig.py', '--env-file', '.env', '--setup-dir', 'data/novig_setup', $Action.ToLowerInvariant())
if ($Action -eq 'prepare') { $arguments += @('--label', $Label) }
if ($Action -in @('open', 'reconcile', 'fund', 'recover-funding', 'export')) {
    if (-not $Journal) { throw 'Provide the journal path printed by the prepare step with -Journal.' }
    $arguments += @('--journal', $Journal)
}
if ($Action -eq 'fund') {
    if (-not $Amount) { throw 'Provide the exact amount with -Amount; no funding amount is chosen automatically.' }
    $arguments += @('--amount', $Amount)
}
if ($Action -eq 'recover-funding') {
    if (-not $Transfer) { throw 'Provide the original client transfer ID with -Transfer.' }
    $arguments += @('--transfer', $Transfer)
}
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'Novig setup did not complete. Review its sanitized message before proceeding.' }
