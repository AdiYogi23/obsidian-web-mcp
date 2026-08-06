$dir     = $PSScriptRoot
$cf      = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
$cfg     = 'C:\Users\AdiYogi\.cloudflared\config.yml'
$logOut  = "$dir\.tunnel.stdout.log"
$logErr  = "$dir\.tunnel.stderr.log"

# Preflight: confirm MCP server still alive on 8420
Write-Output "=== Preflight: MCP server on 8420 ==="
$ns0 = netstat -ano | Select-String ':8420\s.*LISTENING'
if ($ns0) { $ns0 | ForEach-Object { Write-Output ("  " + $_.ToString().Trim()) } }
else {
    Write-Output "  ABORT: nothing listening on 8420 — MCP server is dead, refuse to start tunnel"
    exit
}

# Clean log files
foreach ($f in @($logOut, $logErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

# Start tunnel detached
Write-Output ""
Write-Output "=== Starting tunnel ==="
$proc = Start-Process -FilePath $cf `
    -ArgumentList @('tunnel', '--config', $cfg, 'run', 'vault-mcp') `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -WindowStyle Hidden -PassThru
$tunnelPid = $proc.Id
Write-Output "Tunnel PID: $tunnelPid"

# Wait 10s for connections to establish
for ($i = 1; $i -le 10; $i++) {
    Start-Sleep -Seconds 1
    $proc.Refresh()
    if ($proc.HasExited) {
        Write-Output "Tunnel EXITED after ${i}s (exit code: $($proc.ExitCode)) — aborting"
        break
    }
}

# Check tunnel info
Write-Output ""
Write-Output "=== cloudflared tunnel info vault-mcp ==="
$info = & $cf tunnel info vault-mcp 2>&1
$info | ForEach-Object { Write-Output $_ }

# End-to-end test
Write-Output ""
Write-Output "=== B.2 End-to-end: GET https://brain.hermesx.uk/.well-known/oauth-authorization-server ==="
$url = 'https://brain.hermesx.uk/.well-known/oauth-authorization-server'
$e2eStatus = $null
$e2eBody = $null
try {
    $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop
    $e2eStatus = $r.StatusCode
    $e2eBody = $r.Content
    Write-Output "  HTTP $e2eStatus | Content-Type: $($r.Headers['Content-Type']) | Length: $($e2eBody.Length)"
} catch [System.Net.WebException] {
    $resp = $_.Exception.Response
    if ($resp) {
        $e2eStatus = [int]$resp.StatusCode
        Write-Output "  HTTP $e2eStatus $($resp.StatusDescription)"
        try {
            $sr = New-Object System.IO.StreamReader($resp.GetResponseStream())
            $e2eBody = $sr.ReadToEnd()
            Write-Output "  Body: $e2eBody"
        } catch { }
    } else {
        Write-Output "  WebException (no response): $($_.Exception.Message)"
    }
} catch {
    Write-Output "  Error: $($_.Exception.GetType().Name): $($_.Exception.Message)"
}

# JSON field check
if ($e2eStatus -eq 200 -and $e2eBody) {
    Write-Output ""
    Write-Output "=== OAuth metadata JSON (via tunnel) ==="
    try {
        $json = $e2eBody | ConvertFrom-Json
        $json | ConvertTo-Json -Depth 6
        Write-Output ""
        Write-Output "=== Field check ==="
        $expected = @('issuer', 'authorization_endpoint', 'token_endpoint')
        foreach ($f in $expected) {
            $present = $null -ne $json.PSObject.Properties[$f]
            $val = if ($present) { $json.$f } else { '(missing)' }
            Write-Output ("  {0,-30} present={1,-5}  value={2}" -f $f, $present, $val)
        }
        Write-Output ""
        Write-Output "=== issuer analysis ==="
        Write-Output "  value: $($json.issuer)"
        if ($json.issuer -eq 'https://brain.hermesx.uk') {
            Write-Output "  -> OK: server reports public Cloudflare-fronted HTTPS issuer."
        } elseif ($json.issuer -like 'http://127.0.0.1:*' -or $json.issuer -like 'http://localhost:*') {
            Write-Output "  -> WARN: server still reports local issuer. OAuth clients (claude.ai) will get authorize/token endpoints pointing at localhost from their browser — broken."
        } elseif ($json.issuer -eq 'http://brain.hermesx.uk') {
            Write-Output "  -> WARN: server uses Host header but missed X-Forwarded-Proto -> http scheme, not https. May still work but not strict."
        } else {
            Write-Output "  -> unexpected value"
        }
    } catch {
        Write-Output "JSON parse failed: $_"
    }
}

# Final state
Write-Output ""
Write-Output "=== Tunnel process state ==="
$tunnelAlive = $null -ne (Get-Process -Id $tunnelPid -ErrorAction SilentlyContinue)
Write-Output "Tunnel PID $tunnelPid alive: $tunnelAlive"

Write-Output ""
Write-Output "=== Last 30 lines of .tunnel.stderr.log ==="
if (Test-Path $logErr) {
    Get-Content $logErr -Tail 30 | ForEach-Object { Write-Output ("  " + $_) }
} else { Write-Output "  (no log)" }

Write-Output ""
Write-Output "=== .tunnel.stdout.log ==="
if (Test-Path $logOut) {
    $stdout = Get-Content $logOut -Tail 30
    if ($stdout) { $stdout | ForEach-Object { Write-Output ("  " + $_) } } else { Write-Output "  (empty)" }
} else { Write-Output "  (no log)" }
