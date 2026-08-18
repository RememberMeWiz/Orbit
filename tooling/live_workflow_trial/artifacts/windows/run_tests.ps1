param(
  [string]$Root = ""
)
$ErrorActionPreference = "Stop"
$ArtifactRoot = Split-Path -Parent $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($Root)) {
  $Root = Split-Path -Parent $ArtifactRoot
}
Push-Location $ArtifactRoot
try {
  Write-Host "Orbit Workflow host-independent tests"
  py -3 -m unittest discover -v -s workflow/tests -p "test_*.py"
  if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }

  Write-Host "Orbit native Windows gate tests"
  py -3 -m unittest discover -v -s windows/tests -p "test_*.py"
  if ($LASTEXITCODE -ne 0) { throw "Python command failed with exit code $LASTEXITCODE" }
} finally {
  Pop-Location
}
