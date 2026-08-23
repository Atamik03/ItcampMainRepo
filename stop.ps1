# stop.ps1 -- stop the stack. Containers are removed; data (Postgres bind
# mount + the redis-data/frontend-dist named volumes) is KEPT.
#
# Usage:
#   .\stop.ps1          # stop, keep data (default, safe)
#   .\stop.ps1 -Purge   # ALSO delete the redis-data/frontend-dist volumes
#                       # (Postgres data in .\data\postgres is a host bind
#                       # mount and is NEVER touched by this script -- if
#                       # you also want to wipe it, remove it yourself:
#                       # Remove-Item -Recurse -Force .\data\postgres)
param(
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

if ($Purge) {
    Write-Host "==> Stopping and removing containers + redis-data/frontend-dist volumes..."
    docker compose down -v
    Write-Host "    Postgres data in .\data\postgres was NOT touched (host bind mount)."
} else {
    Write-Host "==> Stopping containers (data preserved)..."
    docker compose down
}
