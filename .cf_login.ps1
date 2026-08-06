$dir = $PSScriptRoot
$cf = 'C:\Program Files (x86)\cloudflared\cloudflared.exe'
$logOut = "$dir\.cf_login.stdout.log"
$logErr = "$dir\.cf_login.stderr.log"

foreach ($f in @($logOut, $logErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}

$proc = Start-Process -FilePath $cf `
    -ArgumentList @('tunnel', 'login') `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr `
    -WindowStyle Hidden -PassThru
Write-Output "cloudflared login PID: $($proc.Id)"

# Wait for URL to appear in output
$urlFound = $false
$url = $null
for ($i = 1; $i -le 15; $i++) {
    Start-Sleep -Seconds 1
    $proc.Refresh()
    if ($proc.HasExited) {
        Write-Output "Login process EXITED after ${i}s (exit code: $($proc.ExitCode)) — unexpected at this stage"
        break
    }
    # Read both logs
    $stdoutContent = if (Test-Path $logOut) { Get-Content $logOut -Raw -ErrorAction SilentlyContinue } else { '' }
    $stderrContent = if (Test-Path $logErr) { Get-Content $logErr -Raw -ErrorAction SilentlyContinue } else { '' }
    $combined = "$stdoutContent`n$stderrContent"
    if ($combined -match 'https://dash\.cloudflare\.com[^\s"]+') {
        $url = $matches[0]
        $urlFound = $true
        Write-Output "Login URL captured after ${i}s"
        break
    }
}

Write-Output ""
Write-Output "=== Login process status ==="
$alive = $null -ne (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)
Write-Output "PID $($proc.Id) alive: $alive (will keep running until cert.pem written or you abort)"

Write-Output ""
Write-Output "=== stdout log ==="
if (Test-Path $logOut) { Get-Content $logOut | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  (empty)" }
Write-Output ""
Write-Output "=== stderr log ==="
if (Test-Path $logErr) { Get-Content $logErr | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  (empty)" }

if ($urlFound) {
    Write-Output ""
    Write-Output "=== AUTH URL ==="
    Write-Output $url
}
