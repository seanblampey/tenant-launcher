<#
.SYNOPSIS
  Builds TenantLauncher.exe (PyInstaller) and the Windows installer (Inno Setup).

  .\build.ps1                 # full build -> installer\TenantLauncher-Setup-<ver>.exe
  .\build.ps1 -SkipInstaller  # just dist\TenantLauncher\

Needs: Python 3.12 with requirements.txt + pyinstaller, bin\bw.exe (Bitwarden CLI, see
Get-BwCli.ps1), and Inno Setup 6 (ISCC.exe) for the installer.
#>
param([switch]$SkipInstaller)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

# Native tools (pip, PyInstaller, ISCC) write progress to stderr; don't let PowerShell treat that as failure.
function Invoke-Native([string]$Label, [scriptblock]$Block) {
    $ErrorActionPreference = 'Continue'
    & $Block 2>&1 | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit $LASTEXITCODE)" }
}

$version = (Select-String -Path common.py -Pattern 'APP_VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Building Tenant Launcher $version" -ForegroundColor Cyan

if (-not (Test-Path 'bin\bw.exe')) { throw "bin\bw.exe missing - run .\Get-BwCli.ps1 first." }

Get-Process TenantLauncher -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Invoke-Native 'pip' { python -m pip install -q --disable-pip-version-check -r requirements.txt pyinstaller }
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }

Invoke-Native 'PyInstaller' {
    python -m PyInstaller --noconfirm --clean --windowed --name TenantLauncher `
        --collect-all playwright `
        --hidden-import pyotp `
        --add-data "customers.example.csv;." `
        --add-data "assets\icon.ico;assets" `
        --add-data "assets\icon-256.png;assets" `
        --icon "assets\icon.ico" `
        --add-binary "bin\bw.exe;bin" `
        --log-level WARN `
        tenant_launcher.py
}
Write-Host "dist\TenantLauncher\TenantLauncher.exe built" -ForegroundColor Green

if ($SkipInstaller) { return }

$iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    (Get-Command ISCC.exe -ErrorAction SilentlyContinue).Source
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup 6 (ISCC.exe) not found. winget install JRSoftware.InnoSetup" }

Invoke-Native 'Inno Setup' { & $iscc /Q "/DAppVersion=$version" installer.iss }
Write-Host "installer\TenantLauncher-Setup-$version.exe ready" -ForegroundColor Green
