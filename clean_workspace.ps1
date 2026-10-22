[CmdletBinding()]
param(
    [switch]$Apply,
    [ValidateRange(0, 8760)][int]$MinAgeHours = 12
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $PSScriptRoot).Path)
$data = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath (Join-Path $root 'data')).Path)
$cutoff = (Get-Date).AddHours(-$MinAgeHours)

if ($Apply) {
    # A pytest run can still have SQLite files open inside its basetemp tree.
    $activeTests = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -match '^(python|pythonw|pytest)(\.exe)?$' -and $_.CommandLine -match '(^|\s)(-m\s+)?pytest(\s|$)'
    })
    if ($activeTests.Count -gt 0) { throw 'A test run is active; retry cleanup after it finishes.' }
}

# Only direct children with known disposable test/benchmark names are selected.
# Operational databases, private credentials, account captures, and trade logs
# are never candidates. pytest-current is reused by run_tests.ps1.
$candidates = @(
    Get-ChildItem -LiteralPath $root -Force -Directory | Where-Object {
        $_.Name -match '^(\.pytest[-_]|pytest_temp)' -and $_.LastWriteTime -le $cutoff
    }
    Get-ChildItem -LiteralPath $data -Force -Directory | Where-Object {
        $_.Name -match '^pytest[-_]' -and $_.Name -ne 'pytest-current' -and
        $_.LastWriteTime -le $cutoff
    }
    Get-ChildItem -LiteralPath $data -Force -File | Where-Object {
        $_.Name -match '^benchmark-[0-9a-f-]+\.db$' -and $_.LastWriteTime -le $cutoff
    }
)

$bytes = [long]0
foreach ($candidate in $candidates) {
    if (($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing a linked cleanup target: $($candidate.Name)"
    }
    $resolved = [IO.Path]::GetFullPath((Resolve-Path -LiteralPath $candidate.FullName).Path)
    $parent = [IO.Path]::GetDirectoryName($resolved)
    if (-not ([string]::Equals($parent, $root, [StringComparison]::OrdinalIgnoreCase) -or
              [string]::Equals($parent, $data, [StringComparison]::OrdinalIgnoreCase))) {
        throw "Cleanup target is outside the workspace: $($candidate.Name)"
    }
    if ($candidate.PSIsContainer) {
        $contents = @(Get-ChildItem -LiteralPath $resolved -Recurse -Force)
        if (@($contents | Where-Object { ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 }).Count -gt 0) {
            throw "Refusing a link inside cleanup target: $($candidate.Name)"
        }
        $files = @($contents | Where-Object { -not $_.PSIsContainer })
        $bytes += [long](($files | Measure-Object -Property Length -Sum).Sum)
    } else {
        $bytes += $candidate.Length
    }
    if ($Apply) {
        if ($candidate.PSIsContainer) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        } else {
            Remove-Item -LiteralPath $resolved -Force
        }
    }
}

$remaining = [long]((Get-ChildItem -LiteralPath $root -Recurse -Force -File |
    Measure-Object -Property Length -Sum).Sum)
[pscustomobject]@{
    applied = [bool]$Apply
    disposable_targets = $candidates.Count
    disposable_bytes = $bytes
    workspace_bytes = $remaining
    under_10_gib = $remaining -lt 10GB
} | ConvertTo-Json -Compress
