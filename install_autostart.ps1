param(
  [Parameter(Mandatory=$true)][string]$Pythonw,
  [Parameter(Mandatory=$true)][string]$ServiceScript
)
$ErrorActionPreference = 'Stop'
$taskName = 'CodexProviderHub'
$pythonResolved = (Resolve-Path -LiteralPath $Pythonw).Path
$scriptResolved = (Resolve-Path -LiteralPath $ServiceScript).Path
$arguments = '"' + $scriptResolved + '" serve'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
  if ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $pythonResolved -or $existing.Actions[0].Arguments -ne $arguments) {
    throw 'A different task with the same name already exists.'
  }
  Write-Output 'already-installed'
  exit 0
}
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $pythonResolved -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
  -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal | Out-Null
$installed = Get-ScheduledTask -TaskName $taskName
if ($installed.Actions.Count -ne 1 -or $installed.Actions[0].Execute -ne $pythonResolved -or $installed.Actions[0].Arguments -ne $arguments) {
  throw 'The startup task did not pass readback.'
}
Write-Output 'installed'
