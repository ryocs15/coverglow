' Starts Coverglow without a console window (double-click)
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
pyw = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\Programs\Python\Python315\pythonw.exe"
If Not fso.FileExists(pyw) Then pyw = "pyw"
sh.Run """" & pyw & """ """ & dir & "\coverglow.py""", 0, False
