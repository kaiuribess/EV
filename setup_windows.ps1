[CmdletBinding()]
param([switch]$SkipFrontend)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Install Python 3.12 or newer from python.org and select Add Python to PATH."
}
& python -c "import sys; assert sys.version_info >= (3,12), 'Python 3.12+ required'"
if ($LASTEXITCODE -ne 0) { throw "Python 3.12+ is required." }
if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
}
if (Test-Path -LiteralPath "requirements-lock.txt") {
    & ".\.venv\Scripts\python.exe" -m pip install -r requirements-lock.txt
    if ($LASTEXITCODE -ne 0) { throw "Locked dependency installation failed." }
}
& ".\.venv\Scripts\python.exe" -m pip install -e ".[test]"
if ($LASTEXITCODE -ne 0) { throw "Backend dependency installation failed." }
if (-not $SkipFrontend) {
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw "Install Node.js 22.12+ (or current LTS) from nodejs.org, then run setup again."
    }
    Push-Location -LiteralPath "frontend"
    try {
        & npm.cmd ci --no-audit --no-fund
        if ($LASTEXITCODE -ne 0) { throw "Frontend dependency installation failed." }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed." }
    } finally { Pop-Location }
}
New-Item -ItemType Directory -Path "data" -Force | Out-Null
if (-not (Test-Path -LiteralPath ".env")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}
Write-Host "Setup complete. Start with .\start_demo.ps1 (no credentials required)." -ForegroundColor Green

