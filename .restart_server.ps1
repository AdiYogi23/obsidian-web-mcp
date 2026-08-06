$dir = $PSScriptRoot
$logOut = "$dir\.server.stdout.log"
$logErr = "$dir\.server.stderr.log"

# --- Step 2a: Stop existing MCP server PIDs ---
Write-Output "=== Step 2a: Stop existing MCP server ==="
$existing = Get-Process | Where-Object { $_.Name -eq 'python' -and $_.StartTime -lt (Get-Date) }
foreach ($p in $existing) {
    Write-Output "  Stopping PID $($p.Id) ($($p.Name), started $($p.StartTime))"
    try {
        Stop-Process -Id $p.Id -ErrorAction Stop
    } catch {
        Write-Output "    SoftKill failed ($($_.Exception.Message)) — escalating to -Force"
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
}

# Wait for port 8420 to free
$portFree = $false
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    $ns = netstat -ano | Select-String ':8420\s.*LISTENING'
    if (-not $ns) { $portFree = $true; Write-Output "  Port 8420 free after ${i}s"; break }
}
if (-not $portFree) {
    Write-Output "  WARN: port 8420 still bound after 10s"
    netstat -ano | Select-String ':8420' | ForEach-Object { Write-Output ("    " + $_.ToString().Trim()) }
}

# --- Step 2b: Rotate old logs ---
Write-Output ""
Write-Output "=== Step 2b: Rotate old logs ==="
foreach ($f in @($logOut, $logErr)) {
    if (Test-Path $f) {
        $prev = $f -replace '\.log$', '.prev.log'
        if (Test-Path $prev) { Remove-Item $prev -Force }
        Move-Item $f $prev -Force
        Write-Output "  $f -> $prev"
    }
}

# --- Step 2c: Load env + start fresh ---
Write-Output ""
Write-Output "=== Step 2c: Start fresh MCP server ==="
$envPath = "$dir\.env"
$envVars = @{}
[System.IO.File]::ReadAllLines($envPath) | ForEach-Object {
    if ($_ -match '^([A-Z_]+)=(.+)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        $envVars[$matches[1]] = $matches[2]
    }
}
Write-Output "  Loaded $($envVars.Count) env vars"

$proc = Start-Process -FilePath 'uv' `
    -ArgumentList @('run', '--directory', $dir, 'vault-mcp') `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -WindowStyle Hidden -PassThru
Write-Output "  Server launcher PID: $($proc.Id)"

# --- Step 2d: Poll localhost readiness (5s timeout per Bundle A lesson) ---
$urlLocal = 'http://127.0.0.1:8420/.well-known/oauth-authorization-server'
$readyLocal = $false
$elapsed = 0
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 1
    $elapsed = $i
    try {
        $r = Invoke-WebRequest -Uri $urlLocal -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($r.StatusCode -eq 200) { $readyLocal = $true; break }
    } catch { }
}
Write-Output "  Localhost OAuth metadata ready after ${elapsed}s: $readyLocal"

# Show new uvicorn worker PID
$workers = Get-Process | Where-Object { $_.Name -eq 'python' } | Sort-Object StartTime -Descending
Write-Output ""
Write-Output "=== New python processes ==="
$workers | Select-Object Id, Name, StartTime, @{n='WS_MB';e={[math]::Round($_.WorkingSet64/1MB,1)}} | Format-Table -AutoSize | Out-String

# --- Step 2e: External probe via brain.hermesx.uk ---
Write-Output "=== Step 2e: External probe via brain.hermesx.uk ==="
$urlExt = 'https://brain.hermesx.uk/.well-known/oauth-authorization-server'
try {
    $r = Invoke-WebRequest -Uri $urlExt -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    Write-Output "  HTTP $($r.StatusCode) | Length: $($r.Content.Length)"
    $json = $r.Content | ConvertFrom-Json
    Write-Output "  issuer: $($json.issuer)"
    Write-Output "  authorization_endpoint: $($json.authorization_endpoint)"
    Write-Output "  token_endpoint: $($json.token_endpoint)"
} catch {
    Write-Output "  EXTERNAL PROBE FAILED: $($_.Exception.Message)"
}

# --- Server logs (first 25 lines) ---
Write-Output ""
Write-Output "=== Fresh server.stderr.log (first 25 lines) ==="
if (Test-Path $logErr) {
    Get-Content $logErr -TotalCount 25 | ForEach-Object { Write-Output ("  " + $_) }
} else { Write-Output "  (no log yet)" }
