# Lance ce script en tant qu'Administrateur une seule fois
# Il programme l'envoi le 20 et la relance le 22 de chaque mois a 8h00

$scriptPath = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = (Get-Command python).Source

$script1 = Join-Path $scriptPath "email_sender.py"
$script2 = Join-Path $scriptPath "relance_sender.py"

# --- Tache 1 : Envoi du releve le 20 ---
schtasks /create /tn "BotMail_Releve" `
    /tr "`"$python`" `"$script1`"" `
    /sc monthly /d 20 /st 08:00 `
    /f

if ($LASTEXITCODE -eq 0) {
    Write-Host "Tache 1 creee : envoi des releves le 20 de chaque mois a 8h00."
} else {
    Write-Host "Erreur creation tache 1."
}

# --- Tache 2 : Relance le 22 ---
schtasks /create /tn "BotMail_Relance" `
    /tr "`"$python`" `"$script2`"" `
    /sc monthly /d 22 /st 08:00 `
    /f

if ($LASTEXITCODE -eq 0) {
    Write-Host "Tache 2 creee : relances le 22 de chaque mois a 8h00."
} else {
    Write-Host "Erreur creation tache 2."
}
