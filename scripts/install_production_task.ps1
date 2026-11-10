[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$hiddenLauncher = Join-Path $PSScriptRoot 'run_production_supervisor_hidden.vbs'
$taskName = 'EV Production Scanner 8002'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    $owned = @($existing.Actions | Where-Object {
        $_.Arguments -like "*ensure_production_running.ps1*" -or
        $_.Arguments -like "*run_production_supervisor_hidden.vbs*"
    })
    if ($owned.Count -ne 1) { throw "A different task already uses $taskName." }
}

$wscript = (Get-Command wscript.exe -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $wscript -WorkingDirectory $projectRoot `
    -Argument ('//B //Nologo "' + $hiddenLauncher + '"')
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$logon = New-ScheduledTaskTrigger -AtLogOn -User $user
$periodic = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 3) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger @($logon, $periodic) `
    -Principal $principal -Settings $settings -Description `
    'Keep the existing EV production scanner running disarmed; recover stopped workers without opening another scanner.' `
    -Force | Out-Null

$installed = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
if ($installed.State -eq 'Disabled') { throw 'Production scanner recovery task was installed disabled.' }
Write-Host "Installed $taskName for $user. It starts at sign-in and checks port 8002 every minute."
