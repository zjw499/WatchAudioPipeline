$ErrorActionPreference = "Stop"

$logonTaskName = "Watch Audio Pipeline"
$startupTaskName = "Watch Audio Pipeline Core"
$startScript = (Resolve-Path (Join-Path $PSScriptRoot "start_services.ps1")).Path
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$logonAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`""
$startupAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -CoreOnly"
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$startupTrigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$logonPrincipal = New-ScheduledTaskPrincipal `
    -UserId $currentUser `
    -LogonType Interactive `
    -RunLevel Limited
$startupPrincipal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $logonTaskName `
    -Action $logonAction `
    -Trigger $logonTrigger `
    -Settings $settings `
    -Principal $logonPrincipal `
    -Description "Verifies the current Apple Watch audio server and starts the user-profile Gemini worker at logon." `
    -Force `
    -ErrorAction Stop | Out-Null

try {
    Register-ScheduledTask `
        -TaskName $startupTaskName `
        -Action $startupAction `
        -Trigger $startupTrigger `
        -Settings $settings `
        -Principal $startupPrincipal `
        -Description "Updates and starts the Apple Watch audio API and transcription worker when Windows starts." `
        -Force `
        -ErrorAction Stop | Out-Null
} catch {
    throw "The logon task was installed, but the boot task requires an elevated PowerShell window. Re-run this script as Administrator. $($_.Exception.Message)"
}

Get-ScheduledTask -TaskName $startupTaskName, $logonTaskName
