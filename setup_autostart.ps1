# ============================================
#  Celito Onboarding Platform - Auto-Start Setup
# ============================================
#  Creates a Windows Task Scheduler task that starts the
#  onboarding server automatically when the server boots.
#
#  - Runs whether or not anyone is logged in
#  - Uses SYSTEM account (no password needed)
#  - Will not create a duplicate if the task already exists
#
#  Run this script once as Administrator on the server:
#    powershell -ExecutionPolicy Bypass -File setup_autostart.ps1
# ============================================

$TaskName    = "CelitoOnboard-AutoStart"
$TaskFolder  = "\Celito"
$ProjectDir  = "C:\Users\celitoadmin\Desktop\Roadmap\HR"
$BatFile     = Join-Path $ProjectDir "start_onboard.bat"
$Description = "Starts the Celito Employee Onboarding & Offboarding Platform (Waitress on port 8780) at system boot. Runs as SYSTEM — no user login required."

# ── Preflight checks ────────────────────────────────────────
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "ERROR: This script must be run as Administrator." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> 'Run as administrator', then try again."
    Write-Host ""
    exit 1
}

if (-not (Test-Path $BatFile)) {
    Write-Host ""
    Write-Host "ERROR: start_onboard.bat not found at:" -ForegroundColor Red
    Write-Host "  $BatFile"
    Write-Host "Make sure the project is deployed to $ProjectDir"
    Write-Host ""
    exit 1
}

# ── Check for existing task ──────────────────────────────────
$existingTask = $null
try {
    $existingTask = Get-ScheduledTask -TaskPath "$TaskFolder\" -TaskName $TaskName -ErrorAction Stop
} catch {}

if ($existingTask) {
    Write-Host ""
    Write-Host "Task '$TaskName' already exists." -ForegroundColor Yellow
    Write-Host "Current state: $($existingTask.State)"
    Write-Host ""
    $answer = Read-Host "Replace it? (y/n)"
    if ($answer -ne 'y') {
        Write-Host "Cancelled."
        exit 0
    }
    Unregister-ScheduledTask -TaskPath "$TaskFolder\" -TaskName $TaskName -Confirm:$false
    Write-Host "Old task removed."
}

# ── Create the scheduled task ────────────────────────────────

# Trigger: at system startup
$trigger = New-ScheduledTaskTrigger -AtStartup

# Action: run start_onboard.bat from the project directory
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$BatFile`"" `
    -WorkingDirectory $ProjectDir

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

# Principal: run as SYSTEM, no login required
$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

# Register
Register-ScheduledTask `
    -TaskPath $TaskFolder `
    -TaskName $TaskName `
    -Description $Description `
    -Trigger $trigger `
    -Action $action `
    -Settings $settings `
    -Principal $principal

Write-Host ""
Write-Host "============================================" -ForegroundColor Green
Write-Host "  Task created successfully!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Task:         $TaskFolder\$TaskName"
Write-Host "  Trigger:      At system startup"
Write-Host "  Runs as:      SYSTEM (no login required)"
Write-Host "  Executes:     $BatFile"
Write-Host "  Working dir:  $ProjectDir"
Write-Host "  Retries:      3 attempts, 1 min apart"
Write-Host ""
Write-Host "To verify, open Task Scheduler and look under:"
Write-Host "  Task Scheduler Library -> Celito -> $TaskName"
Write-Host ""
Write-Host "To test now without rebooting:"
Write-Host "  Start-ScheduledTask -TaskPath '$TaskFolder\' -TaskName '$TaskName'"
Write-Host ""
