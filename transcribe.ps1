$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run setup.ps1 first.'
}
& $python (Join-Path $PSScriptRoot 'transcribe.py') @args
exit $LASTEXITCODE
