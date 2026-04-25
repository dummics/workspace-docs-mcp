param(
    [switch]$All,
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

if ($Dev) {
    python -m pip install -e ".[dev]"
} elseif ($All) {
    python -m pip install -e ".[all]"
} else {
    python -m pip install -e ".[vector,models]"
}

Write-Host "Installed workspace-docs-mcp."
Write-Host "Next: workspace-docs --help"

