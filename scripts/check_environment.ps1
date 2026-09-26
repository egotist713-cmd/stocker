# Проверка окружения Stocker после перезагрузки, восстановления или изменений.
# Только чтение; секреты не выводятся. Запуск (PowerShell):
#     F:\stock\stocker\scripts\check_environment.ps1
# Код выхода = количество FAIL.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$ModelDir = "F:\ai\models\unsloth\Qwen3-VL-8B-Instruct-GGUF"
$LoadConfig = "$env:USERPROFILE\.lmstudio\.internal\user-concrete-model-default-config\unsloth\Qwen3-VL-8B-Instruct-GGUF\Qwen3-VL-8B-Instruct-Q5_K_M.gguf.json"
$script:Failures = 0

function Report([string]$Status, [string]$Name, [string]$Detail = "") {
    if ($Status -eq "FAIL") { $script:Failures++ }
    $color = @{ PASS = "Green"; WARN = "Yellow"; FAIL = "Red" }[$Status]
    Write-Host ("{0} {1}: {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
}

function Check([bool]$Ok, [string]$Name, [string]$Detail = "", [string]$Otherwise = "FAIL") {
    Report ($(if ($Ok) { "PASS" } else { $Otherwise })) $Name $Detail
}

Write-Host "=== Stocker: код и данные"
Push-Location $Root
$dirty = (git status --porcelain | Measure-Object).Count
Check ($dirty -eq 0) "git working tree clean" "$dirty changed files" "WARN"
$ahead = git rev-list --count "origin/main..HEAD" 2>$null
Check ($ahead -eq "0") "git pushed to origin (last known state)" "$ahead local commits not in origin/main" "WARN"
Pop-Location

Check (Test-Path $Python) "python venv" $Python
$envText = if (Test-Path "$Root\.env") { Get-Content "$Root\.env" -Raw } else { "" }
$token = [regex]::Match($envText, '(?m)^STOCKER_MCP_TOKEN=(\S+)').Groups[1].Value
Check ($token.Length -ge 32) ".env STOCKER_MCP_TOKEN" "length $($token.Length)"
Check ($envText -match '(?m)^STOCKER_MCP_HOST=wsl\s*$') ".env STOCKER_MCP_HOST=wsl"
$db = Join-Path $Root "data\db\stocker.db"
if (Test-Path $db) {
    # Путь — отдельным аргументом: встроенный в код "?mode=ro" Windows разбирал как перенаправление.
    $integrity = & $Python -c "import pathlib, sqlite3, sys; uri = pathlib.Path(sys.argv[1]).as_uri() + chr(63) + 'mode' + chr(61) + 'ro'; print(sqlite3.connect(uri, uri=True).execute('pragma quick_check').fetchone()[0])" $db
    Check ($integrity -eq "ok") "SQLite quick_check" $integrity
} else { Report "FAIL" "SQLite database" "missing: $db" }

Write-Host "=== LM Studio и модель"
try {
    $models = (Invoke-RestMethod -Uri "http://127.0.0.1:1234/api/v0/models" -TimeoutSec 10).data
    $vl = $models | Where-Object id -eq "qwen3-vl-8b-instruct"
    Check ($null -ne $vl) "LM Studio API :1234" "qwen3-vl-8b-instruct $($vl.quantization) state=$($vl.state)"
} catch { Report "FAIL" "LM Studio API :1234" $_.Exception.Message }
Check (Test-Path "$ModelDir\mmproj-F16.gguf") "mmproj-F16 in model folder"
Check (-not (Test-Path "$ModelDir\mmproj-F32.gguf")) "no mmproj-F32 in model folder" "(LM Studio must load F16)"
if (Test-Path $LoadConfig) {
    $fields = @{}; (Get-Content $LoadConfig -Raw | ConvertFrom-Json).load.fields | ForEach-Object { $fields[$_.key] = $_.value }
    Check ($fields["llm.load.contextLength"] -eq 32768) "load default: context 32768" "$($fields['llm.load.contextLength'])"
    Check ($fields["llm.load.llama.kCacheQuantizationType"].value -eq "q8_0" -and $fields["llm.load.llama.vCacheQuantizationType"].value -eq "q8_0") "load default: KV cache q8_0"
} else { Report "FAIL" "LM Studio load defaults for qwen3-vl" "missing" }

Write-Host "=== Stocker MCP и автозапуск"
$wslHost = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object InterfaceAlias -like 'vEthernet (WSL*' | Select-Object -First 1).IPAddress
Check ($null -ne $wslHost) "vEthernet (WSL) address" "$wslHost"
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
Check ($null -ne $listener -and $listener.LocalAddress -eq $wslHost) "Stocker MCP listening on WSL host:8765" "$($listener.LocalAddress)"
foreach ($task in "Stocker MCP", "OpenClaw WSL keep-alive", "LMStudioAutoServer") {
    $t = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    $expectRunning = $task -ne "LMStudioAutoServer"
    Check ($null -ne $t -and (-not $expectRunning -or $t.State -eq "Running")) "scheduled task '$task'" "$($t.State)"
}
$wslconfig = if (Test-Path "$env:USERPROFILE\.wslconfig") { Get-Content "$env:USERPROFILE\.wslconfig" -Raw } else { "" }
Check ($wslconfig -notmatch 'networkingMode\s*=\s*mirrored') ".wslconfig without mirrored networking" "(mirrored fails on this machine, 0x8007054f)"

Write-Host "=== OpenClaw (WSL)"
$repoSkill = "/mnt/f/stock/stocker/integrations/openclaw/skills/stocker/SKILL.md"
$lines = wsl -d OpenClawGateway --exec python3 /mnt/f/stock/stocker/scripts/openclaw_check.py $wslHost $repoSkill 2>&1
foreach ($line in $lines) {
    if ($line -match '^(PASS|WARN|FAIL) (.+?): (.*)$') { Report $Matches[1] $Matches[2] $Matches[3] } else { Write-Host "  $line" }
}

Write-Host "=== n8n (Docker Desktop)"
$n8nState = docker inspect -f "{{.State.Status}} {{.HostConfig.RestartPolicy.Name}}" n8n 2>$null
Check ($n8nState -eq "running unless-stopped") "n8n container" "$n8nState"
try {
    $health = (Invoke-WebRequest -Uri "http://127.0.0.1:5678/healthz" -TimeoutSec 5 -UseBasicParsing).StatusCode
    Check ($health -eq 200) "n8n UI 127.0.0.1:5678" "$health"
} catch { Report "FAIL" "n8n UI 127.0.0.1:5678" $_.Exception.Message }
$exposed = Get-NetTCPConnection -LocalPort 5678 -State Listen -ErrorAction SilentlyContinue | Where-Object LocalAddress -notin @("127.0.0.1", "::1")
Check ($null -eq $exposed) "n8n not exposed to LAN" "$($exposed.LocalAddress)"
$fromN8n = docker exec n8n node -e "fetch(process.env.STOCKER_URL + '/api/v1/review.queue', {method: 'POST', body: '{}'}).then(r => console.log(r.status)).catch(() => console.log('ERR'))" 2>$null
Check ($fromN8n -eq "401") "n8n container -> Stocker API (401 without token)" "$fromN8n"
$n8nUrl = docker exec n8n printenv STOCKER_URL 2>$null
Check ($n8nUrl -eq "http://${wslHost}:8765") "n8n STOCKER_URL matches WSL host" "$n8nUrl"
$workflows = (docker exec n8n n8n list:workflow 2>$null) -join " "
$missing = @("stocker-ingest", "stocker-retry", "stocker-digest", "stocker-notify") | Where-Object { $workflows -notmatch $_ }
Check ($missing.Count -eq 0) "n8n workflows imported" "missing: $($missing -join ', ')" "WARN"

Write-Host ""
if ($script:Failures -eq 0) { Write-Host "All checks passed." -ForegroundColor Green } else { Write-Host "$($script:Failures) check(s) FAILED." -ForegroundColor Red }
exit $script:Failures
