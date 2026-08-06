$dir = $PSScriptRoot
$envPath = "$dir\.env"
$logOut = "$dir\.server.stdout.log"
$logErr = "$dir\.server.stderr.log"

foreach ($f in @($logOut, $logErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

$envVars = @{}
[System.IO.File]::ReadAllLines($envPath) | ForEach-Object {
    if ($_ -match '^([A-Z_]+)=(.+)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process')
        $envVars[$matches[1]] = $matches[2]
    }
}
Write-Output "Loaded $($envVars.Count) env vars: $($envVars.Keys -join ', ')"

$proc = Start-Process -FilePath 'uv' `
    -ArgumentList @('run', '--directory', $dir, 'vault-mcp') `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -WindowStyle Hidden -PassThru
$serverPid = $proc.Id
Write-Output "Server PID: $serverPid"

$url = 'http://localhost:8420/.well-known/oauth-authorization-server'
$ready = $false
$status = $null
$body = $null
$elapsed = 0
for ($i = 1; $i -le 30; $i++) {
    Start-Sleep -Seconds 1
    $elapsed = $i
    $proc.Refresh()
    if ($proc.HasExited) {
        Write-Output "Server PID $serverPid EXITED after ${elapsed}s (exit code: $($proc.ExitCode))"
        break
    }
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        $status = $r.StatusCode
        $body = $r.Content
        $ready = $true
        break
    } catch { }
}

Write-Output ""
Write-Output "=== Readiness ==="
Write-Output "Reachable after ${elapsed}s: $ready | HTTP status: $status"

if ($ready) {
    Write-Output ""
    Write-Output "=== OAuth metadata JSON ==="
    try {
        $json = $body | ConvertFrom-Json
        $json | ConvertTo-Json -Depth 5
        Write-Output ""
        $expected = @('issuer', 'authorization_endpoint', 'token_endpoint')
        $present = $expected | Where-Object { $json.PSObject.Properties[$_] }
        $missing = $expected | Where-Object { -not $json.PSObject.Properties[$_] }
        Write-Output "Expected fields present: $($present -join ', ')"
        if ($missing) { Write-Output "MISSING: $($missing -join ', ')" }
    } catch {
        Write-Output "JSON parse failed: $_"
        $b = if ($body) { $body } else { '(empty)' }
        Write-Output ("Raw body (first 500): " + $b.Substring(0, [Math]::Min(500, $b.Length)))
    }
}

Write-Output ""
Write-Output "=== netstat | port 8420 ==="
$ns = netstat -ano | Select-String '8420'
if ($ns) { $ns | Select-Object -First 5 | ForEach-Object { Write-Output ("  " + $_.ToString().Trim()) } }
else { Write-Output "  (no entries on port 8420)" }

Write-Output ""
Write-Output "=== Server stdout (first 20 lines) ==="
if (Test-Path $logOut) {
    $out = Get-Content $logOut -TotalCount 20
    if ($out) { $out | ForEach-Object { Write-Output ("  " + $_) } } else { Write-Output "  (empty)" }
} else { Write-Output "  (no log)" }

Write-Output ""
Write-Output "=== Server stderr (first 20 lines) ==="
if (Test-Path $logErr) {
    $err = Get-Content $logErr -TotalCount 20
    if ($err) { $err | ForEach-Object { Write-Output ("  " + $_) } } else { Write-Output "  (empty)" }
} else { Write-Output "  (no log)" }

$alive = $null -ne (Get-Process -Id $serverPid -ErrorAction SilentlyContinue)
Write-Output ""
Write-Output "Server PID $serverPid alive at end: $alive"

if (-not $ready) {
    if ($alive) {
        Write-Output "Killing zombie (not ready)."
        Stop-Process -Id $serverPid -Force -ErrorAction SilentlyContinue
    }
}
