# scripts/scan-images.ps1
#
# Vulnerability-scans every image used by this stack via the official Trivy
# container image (no local Trivy install required). Run this AFTER
# `docker compose build`.
#
# Usage:
#   docker compose build
#   .\scripts\scan-images.ps1

$ErrorActionPreference = "Continue"

# Compose names locally-built images "<project-dir-name>-<service>" by default,
# where <project-dir-name> is derived from the folder this repo lives in (or
# the COMPOSE_PROJECT_NAME env var / `name:` field if set). The guesses below
# assume the default; if they don't match what you actually built, run:
#   docker compose images
# and substitute the real names/tags below.
$ProjectName = (Split-Path -Leaf (Get-Location)).ToLower() -replace '[^a-z0-9]', '-'

$Images = @(
    "$ProjectName-backend",
    "$ProjectName-frontend-build",
    "$ProjectName-frontend-nginx",
    "postgres:16-alpine",
    "redis:7-alpine"
)

Write-Host "Images to scan (edit this list, or check 'docker compose images', if names don't match):"
$Images | ForEach-Object { Write-Host "  - $_" }
Write-Host ""

foreach ($image in $Images) {
    Write-Host "==================================================================="
    Write-Host "Scanning: $image"
    Write-Host "==================================================================="
    docker run --rm `
        -v /var/run/docker.sock:/var/run/docker.sock `
        -v trivy-cache:/root/.cache/ `
        aquasec/trivy image --severity HIGH,CRITICAL $image
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Scan failed for $image (image may not exist locally -- check 'docker compose images')"
    }
    Write-Host ""
}

Write-Host "Done. For full (non-HIGH/CRITICAL-only) output, re-run trivy without --severity."
