param(
  [string]$EvidenceDirectory = ""
)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "python_launcher.ps1")

$ArtifactRoot = Split-Path -Parent $PSScriptRoot
$PackageRoot = Split-Path -Parent $ArtifactRoot
if ([string]::IsNullOrWhiteSpace($EvidenceDirectory)) {
  $EvidenceDirectory = Join-Path $ArtifactRoot "evidence\native_windows"
}
if (Test-Path $EvidenceDirectory) {
  Remove-Item -Recurse -Force $EvidenceDirectory
}
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null

$manifestPath = Join-Path $ArtifactRoot "workflow_manifest.json"
$manifest = Get-Content -Raw $manifestPath | ConvertFrom-Json
$contentManifestPath = Join-Path $ArtifactRoot "evidence\content_sha256.txt"
$contentManifestHash = if (Test-Path $contentManifestPath) { (Get-FileHash -Algorithm SHA256 $contentManifestPath).Hash.ToLowerInvariant() } else { "missing" }
$adapterPath = Join-Path $ArtifactRoot "windows\adapters\place_packet.py"
$adapterHash = (Get-FileHash -Algorithm SHA256 $adapterPath).Hash.ToLowerInvariant()
$orbitPython = Get-OrbitPython
$pythonVersion = (Invoke-OrbitPython -Arguments @("--version") 2>&1 | Out-String).Trim()
$pythonLauncherKind = $orbitPython.Exe

$osInfo = Get-CimInstance Win32_OperatingSystem | Select-Object Caption, Version, BuildNumber, OSArchitecture
$envRecord = [ordered]@{
  evidence_schema_version = "orbit.native-windows-evidence/0.2"
  captured_at = (Get-Date).ToString("o")
  windows_edition = $osInfo.Caption
  windows_version = $osInfo.Version
  windows_build = $osInfo.BuildNumber
  windows_architecture = $osInfo.OSArchitecture
  computer_name = $env:COMPUTERNAME
  powershell_version = $PSVersionTable.PSVersion.ToString()
  python_runtime_version = $pythonVersion
  python_interpreter_resolution = $pythonLauncherKind
  repository_build_commit_identity = "package-content-manifest-sha256:$contentManifestHash"
  workflow_contract_version = $manifest.schema_version
  workflow_manifest_version = $manifest.schema_version
  test_fixture_version = "orbit.nwin-fixtures/0.2"
  configured_watched_root = $manifest.inbox
  configured_packet_destination_root = $manifest.role_destination_registry.TL.endpoint_ref
  executor_catalog = @($manifest.allowed_executor_operations)
  executor_adapter_sha256 = $adapterHash
  artifact_test_package_hash_manifest_sha256 = $contentManifestHash
  timestamp_authority_note = "traceability-only; timestamps are not workflow ordering authority"
  exact_command = ".\artifacts\windows\run_native_validation.ps1"
}
$envRecord | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $EvidenceDirectory "environment.json")

$env:ORBIT_NATIVE_EVIDENCE_DIR = $EvidenceDirectory
$testLog = Join-Path $EvidenceDirectory "native_test_results.txt"
try {
  & (Join-Path $PSScriptRoot "run_tests.ps1") -Root $PackageRoot *>&1 | Tee-Object -FilePath $testLog
} finally {
  Remove-Item Env:ORBIT_NATIVE_EVIDENCE_DIR -ErrorAction SilentlyContinue
}
if (Select-String -Path $testLog -Pattern "skipped .*native Windows gate" -Quiet) {
  throw "Native gate tests were skipped; this is not valid native Windows evidence."
}
if (-not (Select-String -Path $testLog -Pattern "Ran 14 tests" -Quiet)) {
  throw "Expected the 14-test native gate suite to execute."
}

$gateFiles = @()
for ($i = 1; $i -le 11; $i++) {
  $gateId = "NWIN-{0:D3}" -f $i
  $gatePath = Join-Path $EvidenceDirectory ($gateId + ".json")
  if (-not (Test-Path $gatePath)) { throw "Missing native evidence file: $gateId" }
  $gate = Get-Content -Raw $gatePath | ConvertFrom-Json
  if ($gate.status -ne "PASS") { throw "Native gate did not report PASS: $gateId" }
  $gateFiles += (Split-Path -Leaf $gatePath)
}
$bootstrapGatePath = Join-Path $EvidenceDirectory "LIVE003-NWIN-001.json"
if (-not (Test-Path $bootstrapGatePath)) { throw "Missing native evidence file: LIVE003-NWIN-001" }
$bootstrapGate = Get-Content -Raw $bootstrapGatePath | ConvertFrom-Json
if ($bootstrapGate.status -ne "PASS") { throw "Native bootstrap gate did not report PASS: LIVE003-NWIN-001" }
$gateFiles += (Split-Path -Leaf $bootstrapGatePath)
$bootstrapLauncherGatePath = Join-Path $EvidenceDirectory "LIVE003-NWIN-002.json"
if (-not (Test-Path $bootstrapLauncherGatePath)) { throw "Missing native evidence file: LIVE003-NWIN-002" }
$bootstrapLauncherGate = Get-Content -Raw $bootstrapLauncherGatePath | ConvertFrom-Json
if ($bootstrapLauncherGate.status -ne "PASS") { throw "Native bootstrap launcher gate did not report PASS: LIVE003-NWIN-002" }
$gateFiles += (Split-Path -Leaf $bootstrapLauncherGatePath)
$pythonResolutionGatePath = Join-Path $EvidenceDirectory "LIVE003-NWIN-003.json"
if (-not (Test-Path $pythonResolutionGatePath)) { throw "Missing native evidence file: LIVE003-NWIN-003" }
$pythonResolutionGate = Get-Content -Raw $pythonResolutionGatePath | ConvertFrom-Json
if ($pythonResolutionGate.status -ne "PASS") { throw "Native interpreter resolution gate did not report PASS: LIVE003-NWIN-003" }
$gateFiles += (Split-Path -Leaf $pythonResolutionGatePath)

# Independent PowerShell-to-reconciler smoke proving the launcher and disposable
# workspace path outside unittest. This remains a test harness action, not an
# executor operation available to handoff content.
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("orbit-win002-r2-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Force -Path $tempRoot | Out-Null
try {
  Copy-Item -Recurse -Force $ArtifactRoot (Join-Path $tempRoot "artifacts")
  $workspace = Join-Path $tempRoot "artifacts\sample_workspace"
  Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $workspace "state.json")
  Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $workspace "receipts\receipts.jsonl")
  Get-ChildItem -File -ErrorAction SilentlyContinue (Join-Path $workspace "inbox") | Remove-Item -Force
  Get-ChildItem -File -ErrorAction SilentlyContinue (Join-Path $workspace "outboxes\TL") | Remove-Item -Force

  $handoff = @"
# Orbit Handoff

## Header
- Work Item: M0-WF-WIN-001
- From: WORKER
- To: TL
- Status: COMPLETE
- Handoff ID: native-powershell-smoke-r2
- Sequence: 1

## Executive Summary
Native PowerShell reconciler smoke fixture.
"@
  $incomingTmp = Join-Path $workspace "inbox\incoming.partial"
  $incomingFinal = Join-Path $workspace "inbox\HANDOFF_M0-WF-WIN-001_WORKER_TO_TL.md"
  Set-Content -Path $incomingTmp -Value $handoff -Encoding UTF8 -NoNewline
  Move-Item -Force $incomingTmp $incomingFinal

  $reconcileLog = Join-Path $EvidenceDirectory "reconciler_smoke.txt"
  & (Join-Path $PSScriptRoot "run_reconciler.ps1") -Root $tempRoot -Polls 3 -Interval 0.35 *>&1 | Tee-Object -FilePath $reconcileLog

  $statePath = Join-Path $workspace "state.json"
  if (-not (Test-Path $statePath)) { throw "Reconciler did not create state.json" }
  $state = Get-Content -Raw $statePath | ConvertFrom-Json
  if ($state.current_owner_role -ne "TL") { throw "Reconciler smoke did not advance owner to TL" }
  $packets = @(Get-ChildItem -File (Join-Path $workspace "outboxes\TL") -Filter "NEXT_*.json")
  if ($packets.Count -ne 1) { throw "Expected exactly one prepared TL packet, got $($packets.Count)" }

  Copy-Item -Force $statePath (Join-Path $EvidenceDirectory "reconciler_state.json")
  Copy-Item -Force $packets[0].FullName (Join-Path $EvidenceDirectory "reconciler_packet.json")
  if (Test-Path (Join-Path $workspace "receipts\receipts.jsonl")) {
    Copy-Item -Force (Join-Path $workspace "receipts\receipts.jsonl") (Join-Path $EvidenceDirectory "reconciler_receipts.jsonl")
  }
} finally {
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $tempRoot
}

Push-Location $ArtifactRoot
try {
  Invoke-OrbitPython -Arguments @("-m", "windows.postrun_secret_scan", "--evidence-dir", "$EvidenceDirectory")
  if ($LASTEXITCODE -ne 0) { throw "Post-run trace/secret scan failed with exit code $LASTEXITCODE" }
} finally {
  Pop-Location
}
$postrunScan = Get-Content -Raw (Join-Path $EvidenceDirectory "postrun_secret_scan.json") | ConvertFrom-Json

$traceScan = Get-Content -Raw (Join-Path $EvidenceDirectory "NWIN-011.json") | ConvertFrom-Json
$summary = [ordered]@{
  status = "PASS"
  native_windows_gate_tests = 14
  native_gate_files = $gateFiles
  reconciler_smoke = "PASS"
  allowed_executor_operations = @($manifest.allowed_executor_operations)
  trace_canary_scan_status = $traceScan.trace_canary_scan_result.status
  postrun_evidence_secret_scan_status = $postrunScan.status
  release_blocking_skips = 0
  evidence_directory = $EvidenceDirectory
}
$summary | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $EvidenceDirectory "summary.json")
Write-Host "Native Windows validation PASS. Evidence: $EvidenceDirectory"
