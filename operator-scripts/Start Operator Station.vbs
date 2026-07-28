' Launch start_operator_hidden.ps1 silently via powershell.
' This .vbs wrapper ensures the PowerShell window never appears at all
' (wscript.exe running .vbs with WindowStyle Hidden in PowerShell isn't guaranteed to suppress the flash).
Dim fso, scriptDir, psScript
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
psScript = fso.BuildPath(scriptDir, "start_operator_hidden.ps1")
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & psScript & """", 0, False
