# status.ps1 -- quick health snapshot of the stack.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

Write-Host "==> Container status"
docker compose ps

Write-Host ""
Write-Host "==> Health"
foreach ($svc in @("db", "redis", "backend", "frontend-nginx")) {
    $cid = (docker compose ps -q $svc 2>$null)
    if (-not $cid) {
        Write-Host ("  {0,-16} not running" -f $svc)
        continue
    }
    $status = docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}running (no healthcheck){{end}}' $cid
    Write-Host ("  {0,-16} {1}" -f $svc, $status)
}

Write-Host ""
try {
    $resp = Invoke-WebRequest -Uri "http://localhost:8080/health" -UseBasicParsing -TimeoutSec 3
    Write-Host "==> App reachable at http://localhost:8080 (backend /health -> HTTP $($resp.StatusCode))"
} catch {
    Write-Host "==> App not reachable at http://localhost:8080"
}
