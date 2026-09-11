# ============================================
#  Celito Onboarding Platform - Daily Restart
# ============================================
#  Called by Task Scheduler at boot and daily at 7:00 AM.
#  Kills any existing server process, then starts fresh.
#  Designed to run as SYSTEM (no desktop session).
# ============================================

$ProjectDir = "C:\Users\celitoadmin\Desktop\Roadmap\HR"
$Python     = "C:\Users\celitoadmin\AppData\Local\Programs\Python\Python314\python.exe"
$ServerScript = Join-Path $ProjectDir "backend\server.py"
$LogDir     = Join-Path $ProjectDir "logs"

# Ensure logs directory exists
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$LogDate = Get-Date -Format "yyyy-MM-dd"
$LogFile = Join-Path $LogDir "server_$LogDate.log"

function Write-Log($msg) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$timestamp] $msg"
}

# ── Kill any existing server process ────────────────────────
# Match the full project path so we only kill THIS app, not other Celito apps
$existing = Get-WmiObject Win32_Process -Filter "CommandLine LIKE '%Roadmap\\HR\\backend\\server.py%'" 2>$null |
            Where-Object { $_.ProcessId -ne $PID }

if ($existing) {
    foreach ($proc in $existing) {
        Write-Log "Stopping existing server process (PID $($proc.ProcessId))..."
        try { Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop } catch {}
    }
    Start-Sleep -Seconds 2
    Write-Log "Old process(es) stopped."
} else {
    Write-Log "No existing server process found."
}

# ── Start the server ────────────────────────────────────────
Write-Log "Starting Celito Onboarding Server..."

# Start Python as a detached process so this script can exit quickly
# (Task Scheduler has a 10-minute execution time limit for the restart task)
#
# NOTE: Do NOT redirect stdout/stderr here — the parent script exits after
# a few seconds, which closes the pipes. If the server later writes to the
# now-broken pipe, it crashes. Let output go to NUL instead.
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $Python
$psi.Arguments = $ServerScript
$psi.WorkingDirectory = $ProjectDir
$psi.UseShellExecute = $false
$psi.RedirectStandardOutput = $false
$psi.RedirectStandardError  = $false
$psi.CreateNoWindow = $true

$process = [System.Diagnostics.Process]::Start($psi)

# Give it a few seconds to make sure it starts
Start-Sleep -Seconds 5

if (-not $process.HasExited) {
    Write-Log "Server started successfully (PID $($process.Id))."
} else {
    Write-Log "ERROR: Server exited immediately with code $($process.ExitCode)."
    exit 1
}

Write-Log "Daily restart script complete."
exit 0
