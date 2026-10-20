[CmdletBinding()]
param([int]$Port = 8001, [switch]$NoBrowser, [switch]$Background)
& "$PSScriptRoot\scripts\start_local.ps1" -Mode demo -Port $Port -NoBrowser:$NoBrowser -Background:$Background

