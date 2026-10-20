[CmdletBinding()]
param([int]$Port = 8001, [switch]$AutoTrade, [switch]$NoBrowser, [switch]$Background)
& "$PSScriptRoot\scripts\start_local.ps1" -Mode paper -Port $Port -AutoTrade:$AutoTrade -NoBrowser:$NoBrowser -Background:$Background

