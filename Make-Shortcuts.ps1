<#
.SYNOPSIS
  Optional: builds a "Shortcuts" folder with one double-clickable shortcut per customer
  (straight into their M365 admin centre) plus one for the launcher window.
  Uses the installed TenantLauncher.exe if present, otherwise the Python source.
  Re-run it whenever the customer list changes.
#>
$ErrorActionPreference = 'Stop'

$root   = $PSScriptRoot
$outDir = Join-Path $root 'Shortcuts'

$exe = @(
    (Join-Path $root 'TenantLauncher.exe'),
    "$env:LOCALAPPDATA\Programs\Tenant Launcher\TenantLauncher.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if ($exe) {
    $csv = "$env:LOCALAPPDATA\TenantLauncher\customers.csv"
    $target = $exe; $prefix = ''
} else {
    $csv = Join-Path $root 'customers.csv'
    $target = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $target) { $target = (Get-Command python.exe).Source }
    $prefix = "`"$(Join-Path $root 'tenant_launcher.py')`" "
}
if (-not (Test-Path $csv)) { throw "No customer list at $csv yet - open Tenant Launcher once first." }
$customers = @(Import-Csv $csv | Where-Object { $_.Name -and $_.Name.Trim() -and -not $_.Name.Trim().StartsWith('#') })

if (Test-Path $outDir) { Get-ChildItem $outDir -Filter *.lnk | Remove-Item -Force }
else { New-Item -ItemType Directory -Path $outDir | Out-Null }

$shell = New-Object -ComObject WScript.Shell
function New-Lnk([string]$Path, [string]$Args) {
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath       = $target
    $lnk.Arguments        = "$prefix$Args"
    $lnk.WorkingDirectory = Split-Path $target
    if ($exe) { $lnk.IconLocation = "$exe,0" }
    $lnk.Save()
}

New-Lnk (Join-Path $outDir '_Tenant Launcher.lnk') ''
foreach ($c in $customers) {
    $safe = ($c.Name.Trim() -replace '[\\/:*?"<>|]', '_')
    $name = $c.Name.Trim().Replace('"', '')
    New-Lnk (Join-Path $outDir "$safe.lnk") "--customer `"$name`""
}
Write-Host "Created $($customers.Count + 1) shortcuts in $outDir" -ForegroundColor Green
