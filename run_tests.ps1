[CmdletBinding()]
param([switch]$SkipFrontend)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) { & ".\setup_windows.ps1" }
New-Item -ItemType Directory -Path "data" -Force | Out-Null
# pytest replaces this directory on each run, so repeated checks cannot leave
# a new full copy of temporary test databases every time.
$testTemp = "data\pytest-current"
& ".\.venv\Scripts\python.exe" -m pytest --basetemp=$testTemp --cov=evplatform --cov-report=term-missing --cov-report=json:data/coverage.json
if ($LASTEXITCODE -ne 0) { throw "Backend tests failed." }
if (-not $SkipFrontend) {
    Push-Location -LiteralPath "frontend"
    try {
        & npm.cmd test
        if ($LASTEXITCODE -ne 0) { throw "Frontend control checks failed." }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    } finally { Pop-Location }
}
Write-Host "All offline checks passed. These checks do not establish live API connectivity." -ForegroundColor Green
# Keep previous ad-hoc pytest basetemp trees from accumulating indefinitely.
# The canonical data/pytest-current tree is retained and reused above.
& ".\clean_workspace.ps1" -Apply

