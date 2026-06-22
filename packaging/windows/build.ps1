<#
  Build the IMAP Cleanup Tool Windows installer (by hand).

  Steps it performs:
    1. Download a relocatable python-build-standalone (PBS) "install_only" build
       and extract it to .\python  (skipped if .\python already exists).
    2. Upgrade pip inside that bundled Python.
    3. Compile installer.iss with Inno Setup (ISCC.exe), producing
       dist\imap-cleanup-tool-<Version>-windows-setup.exe.

  Requirements:
    - Inno Setup installed and ISCC.exe on PATH (https://jrsoftware.org/isinfo.php)
    - Internet access (to download PBS; the installer itself also pip-installs online)

  Usage:
    .\build.ps1 -Version 0.36.8
    .\build.ps1 -Version 0.36.8 -PbsUrl "https://github.com/astral-sh/python-build-standalone/releases/download/<TAG>/cpython-3.12.x+<DATE>-x86_64-pc-windows-msvc-install_only.tar.gz"

  PIN THE PBS RELEASE: set $PbsUrl to a specific python-build-standalone release
  asset so every build embeds the same Python. Record the chosen tag in
  packaging/README.md. The "install_only" tarball extracts a top-level "python\"
  folder, which is exactly what installer.iss expects.
#>

param(
  [string]$Version = "0.36.8",
  [string]$PbsUrl  = ""   # REQUIRED: pin a python-build-standalone install_only .tar.gz URL
)

$ErrorActionPreference = "Stop"
$here  = $PSScriptRoot
$pyDir = Join-Path $here "python"

if (-not (Test-Path $pyDir)) {
  if ([string]::IsNullOrWhiteSpace($PbsUrl)) {
    throw "No bundled Python found and -PbsUrl not set. Pass a python-build-standalone 'install_only' .tar.gz URL (see the header of this script)."
  }
  Write-Host "Downloading python-build-standalone..." -ForegroundColor Cyan
  $tar = Join-Path $here "pbs.tar.gz"
  Invoke-WebRequest -Uri $PbsUrl -OutFile $tar
  Write-Host "Extracting..." -ForegroundColor Cyan
  tar -xzf $tar -C $here          # creates .\python
  Remove-Item $tar
}

if (-not (Test-Path (Join-Path $pyDir "python.exe"))) {
  throw "Expected $pyDir\python.exe after extraction. Check the PBS build (must be an 'install_only' Windows x86_64 tarball)."
}

Write-Host "Upgrading pip in the bundled Python..." -ForegroundColor Cyan
& (Join-Path $pyDir "python.exe") -m pip install --upgrade pip

Write-Host "Compiling the installer with Inno Setup..." -ForegroundColor Cyan
$iss = Join-Path $here "installer.iss"
& ISCC.exe "/DMyAppVersion=$Version" $iss

Write-Host "Done. Installer is in $here\dist" -ForegroundColor Green
