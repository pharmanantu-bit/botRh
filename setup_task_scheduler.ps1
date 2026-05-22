# Lance ce script en tant qu'Administrateur une seule fois
# Il programme l'envoi automatique des mails le 20 de chaque mois a 8h00

$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonScript = Join-Path $scriptPath "email_sender.py"

$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "`"$pythonScript`"" `
    -WorkingDirectory $scriptPath

$trigger = New-ScheduledTaskTrigger `
    -Monthly `
    -DaysOfMonth 20 `
    -At "08:00"

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName "BotMailPharmanadie" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "Envoi automatique des releves mensuels aux employes" `
    -RunLevel Highest `
    -Force

Write-Host "Tache planifiee creee avec succes ! Le bot enverra les mails le 20 de chaque mois a 8h00."
