' Stocker UI without a terminal window (passport 35ZZZO).
' Runs scripts\ui_launcher.ps1 hidden and waits for it (the server lives while the launcher lives).
' Log: data\prod\logs\ui.log. Stop: scripts\stop_ui.cmd.
Option Explicit
Dim shell, fso, scriptDir, launcher, command, code
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(scriptDir, "ui_launcher.ps1")
command = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & launcher & """"
code = shell.Run(command, 0, True)
WScript.Quit code
