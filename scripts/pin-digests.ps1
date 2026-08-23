# scripts/pin-digests.ps1
#
# WHY THIS IS A SCRIPT AND NOT HARDCODED DIGESTS:
# Base image digests change every time the upstream maintainers rebuild that
# image (e.g. to ship an OS security patch), even when the tag stays the same.
# Hardcoding today's digest directly into the Dockerfiles/compose file would go
# stale: eventually you'd either be pinned to an old, unpatched image forever,
# or (if the tag is later garbage-collected upstream) pulling a hash that no
# longer resolves at all. This script is meant to be re-run DELIBERATELY, on
# each intentional version bump, by someone with real registry access (which
# the sandbox that authored these Dockerfiles did not have) -- not baked in
# blindly.
#
# Usage: run from the repo root (PowerShell, Docker Desktop running).
#   .\scripts\pin-digests.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# image:tag -> file(s) containing the literal "image:tag" string to replace
$Targets = [ordered]@{
    "python:3.12-slim"                         = @("docker\backend\Dockerfile")
    "node:20-alpine"                           = @("docker\frontend-build\Dockerfile")
    "nginxinc/nginx-unprivileged:1.27-alpine"  = @("docker\frontend-nginx\Dockerfile")
    "postgres:16-alpine"                       = @("docker-compose.yml")
    "redis:7-alpine"                           = @("docker-compose.yml")
}

foreach ($image in $Targets.Keys) {
    Write-Host "==> Pulling $image"
    docker pull $image

    $digest = docker inspect --format='{{index .RepoDigests 0}}' $image
    if ([string]::IsNullOrWhiteSpace($digest)) {
        Write-Warning "Could not resolve a RepoDigest for $image, skipping"
        continue
    }

    # $digest looks like "repo/name@sha256:....."; extract just the @sha256:... part.
    $shaOnly = $digest.Substring($digest.IndexOf("@"))
    $pinned = "$image$shaOnly"
    Write-Host "    $image  ->  $pinned"

    foreach ($file in $Targets[$image]) {
        if (Test-Path $file) {
            # Match "image:tag" plus any PRE-EXISTING "@sha256:..." suffix(es)
            # and replace the whole thing with the freshly resolved pin, so
            # re-running this script against an already-pinned line replaces
            # the old digest instead of appending a second one. The
            # replacement value is also escaped since PowerShell's -replace
            # treats $ specially in the replacement text.
            $pattern = [regex]::Escape($image) + "(@sha256:[0-9a-f]+)*"
            $replacement = $pinned -replace '\$', '$$$$'
            $content = Get-Content $file -Raw
            $newContent = $content -replace $pattern, $replacement
            Set-Content -Path $file -Value $newContent -Encoding utf8 -NoNewline
            Write-Host "    updated $file"
        } else {
            Write-Warning "$file not found, skipping"
        }
    }
}

Write-Host ""
Write-Host "Done. Review the diffs (git diff) before committing pinned digests."
