# Stocker UI: stop / install / uninstall autostart (паспорт §35ZZZO).
#   stop      — остановить интерфейс на порту 8780 (только процесс app.ui.server; MCP и n8n не трогаются)
#   install   — задача планировщика «Stocker UI»: при входе пользователя, скрыто, перезапуск при сбое
#   uninstall — удалить задачу «Stocker UI» (работающий интерфейс не останавливается)
param([Parameter(Mandatory = $true)][ValidateSet("stop", "install", "uninstall")][string]$Action)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Port = 8780
$TaskName = "Stocker UI"
$LogDir = Join-Path $Root "data\prod\logs"

function Write-Log([string]$Text) {
    # Дозапись с общим доступом и повторами (лог одновременно пишет launcher); сбой лога не роняет действие.
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        $mutex = New-Object System.Threading.Mutex($false, "Local\StockerUiLog")
        $held = $false
        try {
            $held = $mutex.WaitOne(2000)  # записи launcher и stop_ui — по очереди, без наложения строк
            $stream = [System.IO.File]::Open((Join-Path $LogDir "ui.log"), [System.IO.FileMode]::Append, [System.IO.FileAccess]::Write,
                                             [System.IO.FileShare]::ReadWrite)
            $writer = New-Object System.IO.StreamWriter($stream, (New-Object System.Text.UTF8Encoding($false)))
            $writer.WriteLine($line)
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

function Stop-UI {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { Write-Host "Stocker UI is not running (port $Port is free)."; return 0 }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)"
    if ($proc.CommandLine -notmatch 'app\.ui\.server') {
        Write-Host "Port $Port is used by another process, not stopped: pid $($proc.ProcessId) $($proc.Name)"
        return 1
    }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Set-Content -Path (Join-Path $LogDir "ui.stop") -Value (Get-Date -Format s)  # launcher завершится с кодом 0
    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($proc.ParentProcessId)" -ErrorAction SilentlyContinue
    Stop-Process -Id $proc.ProcessId -Force
    if ($parent -and $parent.CommandLine -match 'app\.ui\.server') {
        Stop-Process -Id $parent.ProcessId -Force -ErrorAction SilentlyContinue  # лаунчер .venv\Scripts\python.exe
    }
    Write-Log "stop_ui: stopped pid $($proc.ProcessId)"
    Write-Host "Stocker UI stopped (pid $($proc.ProcessId))."
    return 0
}

function Install-Autostart {
    $vbs = Join-Path $Root "scripts\start_ui_hidden.vbs"
    $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`"" -WorkingDirectory $Root
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -Hidden -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    try {
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
            -Principal $principal -Description "Stocker web UI (127.0.0.1:$Port), hidden, restart on failure" -Force | Out-Null
    } catch {
        Write-Host "Failed to create the task: $($_.Exception.Message)"
        Write-Host "If access is denied, run install_ui_autostart.cmd as administrator."
        return 1
    }
    Write-Host "Autostart enabled: task '$TaskName' runs at logon of $user."
    Write-Host "Start now without logging off: Start-ScheduledTask -TaskName '$TaskName' (or scripts\start_ui_hidden.vbs)."
    return 0
}

function Uninstall-Autostart {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Autostart disabled: task '$TaskName' removed. A running UI keeps running (scripts\stop_ui.cmd)."
    } else {
        Write-Host "Task '$TaskName' not found - nothing to do."
    }
    return 0
}

switch ($Action) {
    "stop" { exit (Stop-UI) }
    "install" { exit (Install-Autostart) }
    "uninstall" { exit (Uninstall-Autostart) }
}
