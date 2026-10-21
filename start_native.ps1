[CmdletBinding()]
param(
    [ValidateSet("sandbox", "production")][string]$Environment = "sandbox",
    [int]$Port = 8002,
    [switch]$NoBrowser,
    [switch]$Background
)
# Starts a paused native workspace. No script bypasses launch evidence or arms orders.
& "$PSScriptRoot\scripts\start_local.ps1" -Mode $Environment -Port $Port -NoBrowser:$NoBrowser -Background:$Background
