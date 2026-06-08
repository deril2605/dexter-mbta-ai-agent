[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptPath = Join-Path $PSScriptRoot "deploy.py"

if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $scriptPath @Args
    exit $LASTEXITCODE
}

if (Get-Command python -ErrorAction SilentlyContinue) {
    & python $scriptPath @Args
    exit $LASTEXITCODE
}

throw "Python 3 is required to run deploy\deploy.py. Install Python and retry."
