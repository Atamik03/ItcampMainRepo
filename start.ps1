# start.ps1
#
# One-command local bring-up for the whole Docker Compose stack. See start.sh
# for the step-by-step rationale (kept in sync); this is the native
# PowerShell equivalent for users not using Git Bash / WSL.
#
# Usage:
#   .\start.ps1

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Info($msg) { Write-Host "==> $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "!! $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Error $msg; exit 1 }

# ---- 1. prerequisites ------------------------------------------------------
Info "Checking prerequisites..."
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "docker is not installed or not on PATH. Install Docker Desktop first."
}
try { docker info | Out-Null } catch { Fail "Docker daemon is not reachable. Is Docker Desktop running?" }
try { docker compose version | Out-Null } catch { Fail "docker compose (v2 plugin) is required. Update Docker Desktop." }

# ---- 2. directories ---------------------------------------------------------
Info "Ensuring .\data\postgres exists..."
New-Item -ItemType Directory -Force -Path .\data\postgres | Out-Null

# ---- 3. .env (non-secret config) -------------------------------------------
if (Test-Path .env) {
    Info ".env already exists, leaving it untouched."
} else {
    Info "Creating .env from .env.example..."
    Copy-Item .env.example .env
}

# ---- 4. secrets -------------------------------------------------------------
New-Item -ItemType Directory -Force -Path .\secrets | Out-Null

function New-Secret {
    $bytes = 1..48 | ForEach-Object { Get-Random -Maximum 256 }
    $b64 = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''
    return $b64.Substring(0, [Math]::Min(40, $b64.Length))
}

$newPostgresPassword = $false
if (Test-Path .\secrets\postgres_password.txt) {
    Info "secrets\postgres_password.txt already exists, keeping it (Postgres was initialized with this password)."
} else {
    Info "Generating secrets\postgres_password.txt..."
    New-Secret | Set-Content -Path .\secrets\postgres_password.txt -NoNewline -Encoding utf8
    $newPostgresPassword = $true
}

if (Test-Path .\secrets\redis_password.txt) {
    Info "secrets\redis_password.txt already exists, keeping it."
} else {
    Info "Generating secrets\redis_password.txt..."
    New-Secret | Set-Content -Path .\secrets\redis_password.txt -NoNewline -Encoding utf8
}

if (Test-Path .\secrets\elou_auth_secret.txt) {
    Info "secrets\elou_auth_secret.txt already exists, keeping it."
} else {
    Info "Generating secrets\elou_auth_secret.txt..."
    New-Secret | Set-Content -Path .\secrets\elou_auth_secret.txt -NoNewline -Encoding utf8
}

Info "Refreshing derived secret files (redis_requirepass.conf, database_url.txt, redis_url.txt)..."
$PgPassword = (Get-Content .\secrets\postgres_password.txt -Raw).Trim()
$RedisPassword = (Get-Content .\secrets\redis_password.txt -Raw).Trim()

$envMap = @{}
Get-Content .env | Where-Object { $_ -match '^\s*[A-Za-z_][A-Za-z0-9_]*=' } | ForEach-Object {
    $k, $v = $_ -split '=', 2
    $envMap[$k.Trim()] = $v.Trim()
}
$PostgresUser = if ($envMap.ContainsKey('POSTGRES_USER')) { $envMap['POSTGRES_USER'] } else { 'elou_avt' }
$PostgresDb = if ($envMap.ContainsKey('POSTGRES_DB')) { $envMap['POSTGRES_DB'] } else { 'elou_avt' }

"requirepass $RedisPassword" | Set-Content -Path .\secrets\redis_requirepass.conf -NoNewline -Encoding utf8
"postgresql://${PostgresUser}:${PgPassword}@db:5432/${PostgresDb}" | Set-Content -Path .\secrets\database_url.txt -NoNewline -Encoding utf8
"redis://:${RedisPassword}@redis:6379/0" | Set-Content -Path .\secrets\redis_url.txt -NoNewline -Encoding utf8

if ($newPostgresPassword) {
    Warn "A brand-new Postgres password was generated. If .\data\postgres already contains an initialized cluster from a PREVIOUS password, Postgres will fail to authenticate. Only remove .\data\postgres yourself if you intend to discard existing data."
}

# ---- 5. digest pinning ------------------------------------------------------
$unpinned = $false
foreach ($f in @("docker\backend\Dockerfile", "docker\frontend-build\Dockerfile", "docker\frontend-nginx\Dockerfile")) {
    $fromLines = Select-String -Path $f -Pattern '^FROM '
    foreach ($line in $fromLines) {
        if ($line.Line -notmatch '@sha256:') { $unpinned = $true }
    }
}
$imageLines = Select-String -Path docker-compose.yml -Pattern '^\s*image:'
foreach ($line in $imageLines) {
    if ($line.Line -notmatch '@sha256:') { $unpinned = $true }
}

if (-not $unpinned) {
    Info "Base images already pinned to a digest, skipping scripts\pin-digests.ps1."
} else {
    Info "Pinning base images to an immutable digest (scripts\pin-digests.ps1)..."
    & .\scripts\pin-digests.ps1
}

# ---- 6. build ----------------------------------------------------------------
Info "Building images (docker compose build)..."
docker compose build

# ---- 7. up ---------------------------------------------------------------------
Info "Starting the stack (docker compose up -d)..."
docker compose up -d

# ---- 8. wait for health -------------------------------------------------------
Info "Waiting for services to report healthy..."
$deadline = (Get-Date).AddSeconds(180)
foreach ($svc in @("db", "redis", "backend", "frontend-nginx")) {
    $cid = (docker compose ps -q $svc)
    if (-not $cid) { Fail "Service '$svc' has no container -- did 'docker compose up -d' fail?" }
    while ($true) {
        $status = docker inspect --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' $cid
        if ($status -eq "healthy" -or $status -eq "no-healthcheck") {
            Info "  ${svc}: $status"
            break
        }
        if ($status -eq "unhealthy") {
            Fail "$svc reported unhealthy. Check: docker compose logs $svc"
        }
        if ((Get-Date) -gt $deadline) {
            Fail "Timed out waiting for $svc to become healthy. Check: docker compose logs $svc"
        }
        Start-Sleep -Seconds 2
    }
}

Write-Host ""
Info "Stack is up."
Write-Host "  Open the app:      http://localhost:8080"
Write-Host "  Backend API docs:  http://localhost:8080/docs"
Write-Host "  Status:             .\status.ps1"
Write-Host "  Logs (follow):      docker compose logs -f backend"
Write-Host "  Stop:                .\stop.ps1"
