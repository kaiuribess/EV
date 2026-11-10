Option Explicit

Dim shell, files, scriptDir, supervisor, powershell, command, exitCode
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
scriptDir = files.GetParentFolderName(WScript.ScriptFullName)
supervisor = files.BuildPath(scriptDir, "ensure_production_running.ps1")
powershell = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe")
command = Chr(34) & powershell & Chr(34) & _
    " -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File " & _
    Chr(34) & supervisor & Chr(34) & " -Port 8002"
exitCode = shell.Run(command, 0, True)
WScript.Quit exitCode
