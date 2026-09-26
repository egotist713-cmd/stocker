# Развернуть workflows Stocker в n8n (контейнер "n8n"). Токены не выводятся.
#
#   F:\stock\stocker\scripts\deploy_n8n_workflows.ps1
#
# Требуется: в n8n создан владелец и credential типа Header Auth с именем "Stocker API"
# (Name: Authorization, Value: Bearer <STOCKER_N8N_TOKEN>). Скрипт находит id этого
# credential, подставляет его вместо __STOCKER_CREDENTIAL_ID__ и импортирует
# integrations/n8n/workflows/*.json. Workflows импортируются выключенными:
# включение — осознанное действие в UI n8n.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Source = Join-Path $Root "integrations\n8n\workflows"
$Container = "n8n"
$CredentialName = "Stocker API"

# id credential: экспорт во временный файл внутри контейнера, из него — только id/name/type.
$lookup = @"
n8n export:credentials --all --output=/tmp/stocker-creds.json >/dev/null 2>&1 || exit 1
node -e 'const c = require("/tmp/stocker-creds.json"); const m = c.filter(x => x.name === process.argv[1]); console.log(JSON.stringify(m.map(x => ({ id: x.id, name: x.name, type: x.type }))));' "$CredentialName"
rm -f /tmp/stocker-creds.json
"@
$found = docker exec $Container sh -c $lookup | ConvertFrom-Json
if (-not $found -or $found.Count -ne 1) { throw "Credential '$CredentialName' not found in n8n (or not unique)." }
if ($found[0].type -ne "httpHeaderAuth") { throw "Credential '$CredentialName' must be of type Header Auth (httpHeaderAuth), got $($found[0].type)." }
$credentialId = $found[0].id
Write-Host "Credential '$CredentialName': id=$credentialId"

$staging = Join-Path ([System.IO.Path]::GetTempPath()) "stocker-n8n-workflows"
Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
New-Item -ItemType Directory $staging | Out-Null
foreach ($file in Get-ChildItem $Source -Filter *.json) {
    $text = (Get-Content $file.FullName -Raw -Encoding UTF8).Replace("__STOCKER_CREDENTIAL_ID__", $credentialId)
    [System.IO.File]::WriteAllText((Join-Path $staging $file.Name), $text, (New-Object System.Text.UTF8Encoding $false))
}

docker exec -u root $Container rm -rf /tmp/stocker-workflows | Out-Null
docker cp $staging "${Container}:/tmp/stocker-workflows" | Out-Null
docker exec $Container n8n import:workflow --separate --input=/tmp/stocker-workflows
docker exec -u root $Container rm -rf /tmp/stocker-workflows | Out-Null
Remove-Item -Recurse -Force $staging

Write-Host "=== workflows in n8n"
docker exec $Container n8n list:workflow
