[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$HarnessArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$previousPythonPath = $env:PYTHONPATH
$sourcePath = Join-Path $projectRoot 'src'

try {
    if ([string]::IsNullOrEmpty($previousPythonPath)) {
        $env:PYTHONPATH = $sourcePath
    } else {
        $env:PYTHONPATH = "$sourcePath;$previousPythonPath"
    }
    python -m cvfx_harness @HarnessArgs
    exit $LASTEXITCODE
} finally {
    if ([string]::IsNullOrEmpty($previousPythonPath)) {
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    } else {
        $env:PYTHONPATH = $previousPythonPath
    }
}
