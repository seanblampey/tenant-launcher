<#
.SYNOPSIS
  Puts the Bitwarden CLI (bw.exe) in bin\ so the build can bundle it.
  Uses an existing install (chocolatey / PATH) if present, otherwise downloads the
  official release from github.com/bitwarden/clients.

  .\Get-BwCli.ps1                 # latest
  .\Get-BwCli.ps1 -Version 2026.10.0
#>
param([string]$Version)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force bin | Out-Null

$local = @(
    "$env:ProgramData\chocolatey\lib\bitwarden-cli\tools\bw.exe",
    (Get-Command bw.exe -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path $_) -and (Get-Item $_).Length -gt 10MB } | Select-Object -First 1

if ($local -and -not $Version) {
    Copy-Item $local bin\bw.exe -Force
    Write-Host "Copied existing bw.exe from $local" -ForegroundColor Green
    return
}

if (-not $Version) {
    $rel = Invoke-RestMethod 'https://api.github.com/repos/bitwarden/clients/releases?per_page=50' -Headers @{ 'User-Agent' = 'TenantLauncher-build' }
    $cli = $rel | Where-Object { $_.tag_name -like 'cli-v*' } | Select-Object -First 1
    $Version = $cli.tag_name -replace '^cli-v', ''
}
$url = "https://github.com/bitwarden/clients/releases/download/cli-v$Version/bw-windows-$Version.zip"
Write-Host "Downloading $url"
$zip = Join-Path $env:TEMP "bw-windows-$Version.zip"
Invoke-WebRequest $url -OutFile $zip -Headers @{ 'User-Agent' = 'TenantLauncher-build' }
Expand-Archive $zip -DestinationPath bin -Force
Remove-Item $zip
Write-Host "bin\bw.exe = Bitwarden CLI $Version" -ForegroundColor Green
