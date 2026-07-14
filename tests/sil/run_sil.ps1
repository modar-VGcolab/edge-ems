<#
  One-command SIL run (Gate G2) for Windows / PowerShell.
  Native equivalent of run_sil.sh -- no WSL or bash required.

  Brings up the full stack (influx + mosquitto + core + controller +
  4 simulators), verifies the simulators came up, waits for the controller
  /health endpoint, runs the seven scenarios, then tears down.

  Usage:   powershell -ExecutionPolicy Bypass -File tests\sil\run_sil.ps1
  Run from the repo root (edge-ems\) or anywhere -- it cd's to the repo root.
#>

$ErrorActionPreference = "Stop"
# Don't let a native command's stderr / non-zero exit abort the script; we check
# $LASTEXITCODE explicitly where it matters (PowerShell 7.3+ would otherwise throw).
$PSNativeCommandUseErrorActionPreference = $false

# --- repo root (this script lives in tests/sil/) ----------------------------
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

# --- prefer the project venv's Python -----------------------------------------
# The test deps and the editable packages (common, core, controller, simulator)
# live in .venv. Pin to it so the run doesn't depend on whether the shell has
# the venv activated (a bare `python` may resolve to a system interpreter that
# lacks `common`, giving ModuleNotFoundError in conftest).
$Py = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host ">> .venv not found at $Py -- falling back to 'python' on PATH"
    $Py = "python"
}

$Compose = "deploy/docker-compose.yml"
$Sims    = @("sim-grid", "sim-bess", "sim-pv", "sim-load", "sim-meter")

# --- activate the 'sil' profile for ALL compose calls in this process -------
# The simulators (and core/controller) live behind the 'sil' profile; without
# this, `docker compose ps` won't list them and the health gate sees 'missing'.
$env:COMPOSE_PROFILES = "sil"

# --- ensure configs/.env exists (containers load it via env_file) -----------
if (-not (Test-Path "configs/.env")) {
    Write-Host ">> configs/.env missing -- creating it from configs/.env.example"
    Copy-Item "configs/.env.example" "configs/.env"
}

# --- INFLUX_TOKEN: use the SAME token as core/controller + the InfluxDB ------
# container, otherwise the pytest InfluxDB client gets 401. Precedence:
#   1) INFLUX_TOKEN already in the environment (explicit override)
#   2) INFLUX_TOKEN from configs/.env (what the containers actually load)
#   3) 'change-me' fallback (matches run_sil.sh)
if (-not $env:INFLUX_TOKEN) {
    $tokenLine = Select-String -Path "configs/.env" -Pattern '^\s*INFLUX_TOKEN\s*=' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tokenLine) {
        $val = ($tokenLine.Line -replace '^\s*INFLUX_TOKEN\s*=\s*', '')
        $val = ($val -replace '\s+#.*$', '').Trim().Trim('"').Trim("'")
        if ($val) { $env:INFLUX_TOKEN = $val }
    }
}
if (-not $env:INFLUX_TOKEN) { $env:INFLUX_TOKEN = "change-me" }
Write-Host ">> INFLUX_TOKEN resolved (length=$($env:INFLUX_TOKEN.Length))"

function Invoke-Teardown {
    docker compose -f $Compose --profile sil down
}

Write-Host ">> building & starting stack (influx + mosquitto + core + controller + simulators)"
docker compose -f $Compose --profile sil up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host ">> compose up failed."; exit 1 }

# --- health gate: every simulator must be 'running' -------------------------
Write-Host ">> health-gate: verifying simulator containers"
Start-Sleep -Seconds 5   # brief settle so an immediate crash shows as 'exited'
$failed = $false
foreach ($svc in $Sims) {
    $cid = (docker compose -f $Compose --profile sil ps -aq $svc 2>$null)
    $status = ""
    if ($cid) { $status = (docker inspect -f '{{.State.Status}}' $cid 2>$null) }
    if ($status -ne "running") {
        $shown = if ($status) { $status } else { "missing" }
        Write-Host "   !! $svc not running (status='$shown')"
        if ($cid) {
            # docker writes the traceback to stderr; merging it with 2>&1 under
            # ErrorActionPreference='Stop' would abort the script, so relax it here.
            $prevEAP = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            $log = (docker logs --tail 25 $cid 2>&1 | Out-String)
            $ErrorActionPreference = $prevEAP
            ($log -split "`r?`n") | ForEach-Object { Write-Host "      | $_" }
        }
        $failed = $true
    } else {
        Write-Host "   ok $svc"
    }
}
if ($failed) {
    Write-Host ">> HEALTH-GATE FAILED: a simulator did not come up. Tearing down."
    Invoke-Teardown
    exit 1
}

# --- wait for controller /health --------------------------------------------
Write-Host ">> waiting for controller /health"
$healthy = $false
for ($i = 1; $i -le 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:5000/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}
if (-not $healthy) {
    Write-Host ">> controller never became healthy. Tearing down."
    Invoke-Teardown
    exit 1
}

# --- run the scenarios ------------------------------------------------------
Write-Host ">> running SIL scenarios"
$env:EDGE_EMS_SIL = "1"
& $Py -m pytest tests/sil/test_sil.py -v
$rc = $LASTEXITCODE

# --- teardown ---------------------------------------------------------------
Write-Host ">> tearing down"
Invoke-Teardown

exit $rc
