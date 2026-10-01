# Stocker UI launcher (скрытый запуск, паспорт §35ZZZO).
# Вызывается из start_ui_hidden.vbs. Лог: data\prod\logs\ui.log (дописывается, ротация по размеру).
# Порт 8780 занят нашим интерфейсом -> выход 0; чужим процессом -> запись в лог, выход 2.
# Сервер работает, пока жив этот процесс (для планировщика: «перезапуск при сбое»).
# Остановка через stop_ui.cmd оставляет метку ui.stop -> выход 0 (планировщик не перезапускает).
# Вывод сервера пишется в лог построчно с общим доступом к файлу: лог не блокируется,
# второй запуск и stop_ui.cmd могут дописывать в него, пока сервер работает.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Port = 8780
$LogDir = Join-Path $Root "data\prod\logs"
$Log = Join-Path $LogDir "ui.log"
$StopMarker = Join-Path $LogDir "ui.stop"
$MaxLogBytes = 5MB
$KeepLogs = 3

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Write-Line([string]$Text) {
    # Открыть на дозапись с общим доступом; при занятости — повторить (до ~2 с). Лог не должен ронять запуск.
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $mutex = New-Object System.Threading.Mutex($false, "Local\StockerUiLog")
        $held = $false
        try {
            $held = $mutex.WaitOne(2000)  # записи launcher и stop_ui — по очереди, без наложения строк
            $stream = [System.IO.File]::Open($Log, [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write,
                                             [System.IO.FileShare]::ReadWrite)
            $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
            $writer.WriteLine($Text)
            $writer.Close()
            return
        } catch {
            Start-Sleep -Milliseconds 100
        } finally {
            if ($held) { $mutex.ReleaseMutex() }
            $mutex.Dispose()
        }
    }
}

function Write-Log([string]$Text) {
    Write-Line ("[{0}] launcher: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text)
}

# Ротация: ui.log -> ui.log.1 -> ... -> ui.log.3 (самый старый удаляется). Сбой ротации запуск не останавливает.
try {
    if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt $MaxLogBytes)) {
        for ($i = $KeepLogs; $i -ge 1; $i--) {
            $older = "$Log.$i"
            if ($i -eq $KeepLogs -and (Test-Path $older)) { Remove-Item $older -Force }
            $newer = if ($i -eq 1) { $Log } else { "$Log.$($i - 1)" }
            if (Test-Path $newer) { Move-Item $newer $older -Force }
        }
    }
} catch {
    Write-Log "log rotation skipped: $($_.Exception.Message)"
}

# Второй запуск: кто слушает порт?
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $ours = $false
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/healthz" -TimeoutSec 5
        $ours = $health.ok -and (($health.data_dir -replace '\\', '/') -like "*/data/prod")
    } catch { $ours = $false }
    if ($ours) {
        Write-Log "already running on 127.0.0.1:$Port (pid $($listener.OwningProcess)) - nothing to do"
        exit 0
    }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" -ErrorAction SilentlyContinue
    Write-Log "port $Port is busy by another process: pid $($listener.OwningProcess) $($proc.Name) $($proc.CommandLine) - UI not started"
    exit 2
}

if (Test-Path $StopMarker) { Remove-Item $StopMarker -Force }
Write-Log "starting UI (scripts\start_ui.cmd)"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"
$ErrorActionPreference = "Continue"
# stderr объединяется со stdout внутри cmd: в PowerShell приходят только строки.
& cmd.exe /c "`"$Root\scripts\start_ui.cmd`" 2>&1" | ForEach-Object { Write-Line "$_" }
$code = $LASTEXITCODE

if (Test-Path $StopMarker) {
    Remove-Item $StopMarker -Force -ErrorAction SilentlyContinue
    Write-Log "stopped by stop_ui.cmd (exit $code)"
    exit 0
}
Write-Log "UI exited with code $code"
exit $code
