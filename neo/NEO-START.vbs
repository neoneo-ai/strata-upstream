' NEO-START.vbs - open the Neo Strata window with no console window behind it.
' Double-click it from Explorer; NEO-START.bat is the same thing in a terminal.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root

pyw = root & "\.venv\Scripts\pythonw.exe"
If fso.FileExists(pyw) Then
    ' -m neo.manager: the manager uses package-relative imports, so running the .py path fails.
    cmd = """" & pyw & """ -m neo.manager"
Else
    cmd = "cmd /c cd /d """ & root & """ && py -3 -m neo.manager"
End If
sh.Run cmd, 0, false
