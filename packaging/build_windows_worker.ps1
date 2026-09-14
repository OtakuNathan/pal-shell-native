param(
    [Parameter(Mandatory=$true)][string]$PythonRoot,
    [Parameter(Mandatory=$true)][string]$NativeBuild,
    [Parameter(Mandatory=$true)][string]$Destination
)
$ErrorActionPreference = 'Stop'
$source = Split-Path $PSScriptRoot -Parent
if (Test-Path $Destination) { throw 'Choose a new bundle destination' }
New-Item -ItemType Directory -Path $Destination | Out-Null
# Use a dedicated, complete Python distribution with msgpack/cryptography installed.
# The NuGet Python package includes the interpreter and standard library.
Copy-Item $PythonRoot (Join-Path $Destination 'python') -Recurse
$python = Join-Path $Destination 'python'
Copy-Item (Join-Path $NativeBuild '_pal_shell_runtime*.pyd') $python
Copy-Item (Join-Path $NativeBuild '_pal_shell_rpc*.pyd') $python
Copy-Item (Join-Path $source 'python/pal_shell_worker') $python -Recurse
Copy-Item (Join-Path $source 'THIRD_PARTY.md') $Destination
Copy-Item (Join-Path $source 'LICENSE') $Destination
@'
@echo off
"%~dp0python\python.exe" -m pal_shell_worker %*
'@ | Set-Content (Join-Path $Destination 'pal-shell-worker.cmd') -Encoding ASCII
& (Join-Path $python 'python.exe') -c 'import _pal_shell_runtime, _pal_shell_rpc, pal_shell_worker, cryptography, msgpack'
if ($LASTEXITCODE) { throw 'Bundle import verification failed' }
Write-Output "Windows prototype bundle written: $Destination"
