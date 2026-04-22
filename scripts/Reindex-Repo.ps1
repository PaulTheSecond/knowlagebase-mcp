#Requires -Version 5.1
<#
.SYNOPSIS
    Triggers re-indexing of a repository in the knowledge-base server.

.DESCRIPTION
    Sends an HTTP POST request to the /sync endpoint of the running knowledge-mcp server.
    Indexing is executed in the background (the server returns immediately).

    IMPORTANT: The repository path must be specified as a path INSIDE THE CONTAINER (/repos/...),
    not as a host path (C:\Repos\...).

    Path mapping:
      If REPOS_DIR=C:\Repos (in .env), then C:\Repos\MyLib on the host
      is accessible inside the container as /repos/MyLib.

.PARAMETER RepoId
    Unique repository identifier used as a key in the database.
    Default: ImpactOS.Core.Lib

.PARAMETER RepoPath
    Path to the repository INSIDE the Docker container.
    Default: /repos/ImpactOS.Core.Lib

.PARAMETER ServerUrl
    URL of the knowledge-mcp HTTP server.
    Default: http://localhost:8000

.PARAMETER Wait
    Stream container logs after triggering sync (Ctrl+C to stop).
    Default: $false (trigger and return).

.EXAMPLE
    # Re-index ImpactOS.Core.Lib with defaults (REPOS_DIR=C:\Repos, container path = /repos/ImpactOS.Core.Lib)
    .\Reindex-Repo.ps1

.EXAMPLE
    # Trigger and watch live progress
    .\Reindex-Repo.ps1 -Wait

.EXAMPLE
    # Index a different repository
    .\Reindex-Repo.ps1 -RepoId "MyOtherLib" -RepoPath "/repos/MyOtherLib"

.EXAMPLE
    # Point to a remote server
    .\Reindex-Repo.ps1 -ServerUrl "http://192.168.1.100:8000" -Wait
#>

param(
    [string]$RepoId    = "ImpactOS.Core.Lib",
    [string]$RepoPath  = "/repos/ImpactOS.Core.Lib",
    [string]$ServerUrl = "http://localhost:8000",
    [switch]$Wait
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ─── Helper functions ────────────────────────────────────────────────────────

function Write-Step([string]$msg) {
    Write-Host "  -> $msg" -ForegroundColor Cyan
}

function Write-Success([string]$msg) {
    Write-Host "  OK $msg" -ForegroundColor Green
}

function Write-Fail([string]$msg) {
    Write-Host "  FAIL $msg" -ForegroundColor Red
}

# ─── Server availability check ───────────────────────────────────────────────

Write-Host ""
Write-Host "==================================================" -ForegroundColor DarkCyan
Write-Host "       Knowledge Base -- Reindex Trigger          " -ForegroundColor DarkCyan
Write-Host "==================================================" -ForegroundColor DarkCyan
Write-Host ""
Write-Step "Server:     $ServerUrl"
Write-Step "Repo ID:    $RepoId"
Write-Step "Repo Path:  $RepoPath  (inside container)"
Write-Host ""

# Check the server is alive
try {
    $null = Invoke-RestMethod -Uri "$ServerUrl/docs" -Method GET -TimeoutSec 5 -ErrorAction Stop
}
catch {
    # /docs may return HTML — that's fine, as long as the server responds at all
    if ($_.Exception.Response -and $_.Exception.Response.StatusCode -ne $null) {
        # Server responded — it's alive
    }
    else {
        Write-Fail "Server $ServerUrl is not reachable. Make sure the knowledge-mcp container is running."
        Write-Host "  Start it with: docker compose up -d  (in c:\Repos\knowledgebase-mcp)" -ForegroundColor Yellow
        exit 1
    }
}

# ─── Send sync request ───────────────────────────────────────────────────────

$body = @{
    repo_id   = $RepoId
    repo_path = $RepoPath
} | ConvertTo-Json

Write-Step "Sending indexing request..."

try {
    $response = Invoke-RestMethod `
        -Uri          "$ServerUrl/sync" `
        -Method       POST `
        -ContentType  "application/json" `
        -Body         $body `
        -TimeoutSec   30

    Write-Success "Request accepted by server!"
    Write-Host ""
    Write-Host "  Status:     " -NoNewline; Write-Host $response.status    -ForegroundColor Yellow
    Write-Host "  Repo ID:    " -NoNewline; Write-Host $response.repo_id   -ForegroundColor White
    Write-Host "  Background: " -NoNewline; Write-Host $response.background -ForegroundColor White
    Write-Host ""
}
catch {
    Write-Fail "Request failed: $_"
    exit 1
}

# ─── Container log streaming (optional) ──────────────────────────────────────

if ($Wait) {
    Write-Host "  Streaming container logs (Ctrl+C to stop)..." -ForegroundColor DarkYellow
    Write-Host "  ─────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host ""

    # Show logs from the last 10 seconds to capture the start of this sync
    docker logs -f --since "10s" knowledge-mcp
}
else {
    Write-Host "  Indexing is running in the background." -ForegroundColor DarkGray
    Write-Host "  To watch progress: " -NoNewline
    Write-Host "docker logs -f knowledge-mcp" -ForegroundColor Yellow
    Write-Host ""
}
