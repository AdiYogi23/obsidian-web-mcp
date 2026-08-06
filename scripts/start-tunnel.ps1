# start-tunnel.ps1 — Task Scheduler entry-point for cloudflared tunnel vault-mcp.
# Foreground wrapper: blocks until cloudflared exits so TS can detect failures + restart.

$ErrorActionPreference = 'Stop'

$dir     = Split-Path -Parent $PSScriptRoot
$logsDir = "$dir\logs"
$cfExe   = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
$cfgPath = 'C:\Users\AdiYogi\.cloudflared\config.yml'

if (-not (Test-Path $logsDir)) { New-Item -ItemType Directory -Path $logsDir -Force | Out-Null }

# 7-day log rotation
Get-ChildItem $logsDir -Filter 'tunnel-*' -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$ts     = Get-Date -Format 'yyyy-MM-dd_HH-mm-ss'
$logOut = "$logsDir\tunnel-$ts.log"
$logErr = "$logsDir\tunnel-$ts.err.log"
$utf8   = [System.Text.UTF8Encoding]::new($false)

function Append-Err([string]$line) {
    [System.IO.File]::AppendAllText($logErr, $line + "`n", $utf8)
}

# Idempotency: don't spawn second cloudflared instance
$existing = Get-Process -Name cloudflared -ErrorAction SilentlyContinue
if ($existing) {
    Append-Err "[$ts] cloudflared already running (PID $(($existing.Id) -join ',')) — exiting 0"
    exit 0
}

# Verify binary + config
if (-not (Test-Path $cfExe)) {
    Append-Err "[$ts] FATAL: cloudflared not found at $cfExe"
    exit 99
}
if (-not (Test-Path $cfgPath)) {
    Append-Err "[$ts] FATAL: config.yml missing at $cfgPath"
    exit 1
}

# Sanity warning: MCP origin should be up. Not fatal — cloudflared retries internally.
$mcpUp = netstat -ano | Select-String ':8420\s.*LISTENING'
if (-not $mcpUp) {
    Append-Err "[$ts] WARN: origin localhost:8420 not LISTENING yet — cloudflared will retry until it is"
}

# Foreground exec
$proc = Start-Process -FilePath $cfExe `
    -ArgumentList @('tunnel', '--config', $cfgPath, 'run', 'vault-mcp') `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -NoNewWindow -Wait -PassThru
$ec = $proc.ExitCode

Append-Err "[$(Get-Date -Format 'yyyy-MM-dd_HH-mm-ss')] cloudflared exited with code $ec"
exit $ec
