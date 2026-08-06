# One-shot script to register both Task Scheduler tasks.
# Idempotent — uses -Force to overwrite existing entries.

$dir = Split-Path -Parent $PSScriptRoot
$mcpServerScript = Join-Path $dir 'scripts\start-mcp-server.ps1'
$tunnelScript = Join-Path $dir 'scripts\start-tunnel.ps1'

$userId = "$env:COMPUTERNAME\$env:USERNAME"
Write-Output "Registering tasks for user: $userId"

# --- Shared settings (battery-tolerant, no time limit, restart on failure) ---
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

# --- Principal: current user, S4U (no password stored), Limited (non-elevated) ---
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType S4U `
    -RunLevel Limited

# --- Task A: ObsidianMCP-Server (no delay) ---
$actionA = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$mcpServerScript`""

$triggerA = New-ScheduledTaskTrigger -AtStartup

Register-ScheduledTask `
    -TaskName 'ObsidianMCP-Server' `
    -Action $actionA `
    -Trigger $triggerA `
    -Settings $settings `
    -Principal $principal `
    -Description "Auto-start Obsidian MCP server (uv run vault-mcp). Wrapper: $mcpServerScript" `
    -Force | Out-Null

Write-Output "  Registered: ObsidianMCP-Server"

# --- Task B: ObsidianMCP-Tunnel (30s startup delay) ---
$actionB = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$tunnelScript`""

$triggerB = New-ScheduledTaskTrigger -AtStartup
$triggerB.Delay = 'PT30S'

Register-ScheduledTask `
    -TaskName 'ObsidianMCP-Tunnel' `
    -Action $actionB `
    -Trigger $triggerB `
    -Settings $settings `
    -Principal $principal `
    -Description "Auto-start cloudflared tunnel vault-mcp (30s delay after MCP-Server). Wrapper: $tunnelScript" `
    -Force | Out-Null

Write-Output "  Registered: ObsidianMCP-Tunnel"

Write-Output ""
Write-Output "=== Final state ==="
Get-ScheduledTask -TaskName 'ObsidianMCP-*' | Select-Object TaskName, State, @{n='Principal';e={$_.Principal.UserId}}, @{n='LogonType';e={$_.Principal.LogonType}} | Format-Table -AutoSize | Out-String

Write-Output "=== Triggers ==="
Get-ScheduledTask -TaskName 'ObsidianMCP-*' | ForEach-Object {
    $t = $_.Triggers[0]
    Write-Output ("  {0}: {1}  Delay={2}  Enabled={3}" -f $_.TaskName, $t.CimClass.CimClassName, $t.Delay, $t.Enabled)
}
