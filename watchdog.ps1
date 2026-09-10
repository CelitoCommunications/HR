# ============================================
#  Celito Onboarding Platform - Watchdog
# ============================================
#  Monitors the server process and restarts it if it crashes.
#  Run by Task Scheduler alongside the main startup task.
#
#  Checks every 30 seconds if server.py is running.
#  If not, restarts it and logs the event.
#  Exits cleanly if a stop_onboard signal file is found.
# ============================================

$ProjectDir   = "C:\Users\celitoadmin\Desktop\Roadmap\HR"
$Python       = "C:\Users\celitoadmin\AppData\Local\Programs\Python\Python314\python.exe"
$ServerScript = Join-Path $ProjectDir "backend\server.py"
$LogDir       = Join-Path $ProjectDir "logs"
$StopSignal   = Join-Path $ProjectDir ".stop_watchdog"
$CheckInterval = 30   # seconds between checks
$MaxRestarts   = 10   # max restarts per day before giving up

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

$LogDate = Get-Date -Format "yyyy-MM-dd"
$LogFile = Join-Path $LogDir "watchdog_$LogDate.log"
$restartCount = 0

function Write-Log($msg) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $LogFile -Value "[$timestamp] $msg"
}

function Is-ServerRunning {
    $procs = Get-WmiObject Win32_Process -Filter "CommandLine LIKE '%backend\\server.py%'" 2>$null |
             Where-Object { $_.ProcessId -ne $PID }
    return ($null -ne $procs -and @($procs).Count -gt 0)
}

function Start-Server {
    Write-Log "Starting server..."
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Python
    $psi.Arguments = $ServerScript
    $psi.WorkingDirectory = $ProjectDir
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError  = $false
    $psi.CreateNoWindow = $true

    $process = [System.Diagnostics.Process]::Start($psi)
    Start-Sleep -Seconds 5

    if (-not $process.HasExited) {
        Write-Log "Server started (PID $($process.Id))."
        return $true
    } else {
        Write-Log "ERROR: Server exited immediately with code $($process.ExitCode)."
        return $false
    }
}

# ── Main loop ──────────────────────────────────────────────
Write-Log "Watchdog started. Checking every ${CheckInterval}s. Max restarts/day: $MaxRestarts."

while ($true) {
    # Check for stop signal
    if (Test-Path $StopSignal) {
        Write-Log "Stop signal found. Watchdog exiting."
        Remove-Item $StopSignal -Force -ErrorAction SilentlyContinue
        exit 0
    }

    # Roll log file at midnight
    $today = Get-Date -Format "yyyy-MM-dd"
    if ($today -ne $LogDate) {
        $LogDate = $today
        $LogFile = Join-Path $LogDir "watchdog_$LogDate.log"
        $restartCount = 0
        Write-Log "New day — restart counter reset."
    }

    if (-not (Is-ServerRunning)) {
        Write-Log "WARNING: Server is not running!"

        if ($restartCount -ge $MaxRestarts) {
            Write-Log "ERROR: Max restarts ($MaxRestarts) reached today. Giving up — check the server manually."
            Start-Sleep -Seconds 300  # wait 5 min before checking again
            continue
        }

        $restartCount++
        Write-Log "Restart attempt $restartCount of $MaxRestarts..."

        if (Start-Server) {
            Write-Log "Server recovered successfully."
        } else {
            Write-Log "Server failed to start. Will retry in ${CheckInterval}s."
        }
    }

    Start-Sleep -Seconds $CheckInterval
}
